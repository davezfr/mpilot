from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from mpilot.daemon import acquire_daemon_lock, release_daemon_lock, run_daemon, run_daemon_once
from mpilot.mcp.qbitlarr_notifications import DownloadCompletionNotifier, DownloadWatchStore
from mpilot.runtime import MediaWorkflowRuntime
from mpilot.runtime.dispatcher import dispatch_qbitlarr_completion


class FakeQbitlarrClient:
    async def get_download_status(self, _info_hash):
        return {
            "name": "Example.Movie.2026.1080p.WEB-DL.H.264-GRP",
            "state": "uploading",
            "progress": 1.0,
            "hash": "abc123",
            "content_path": "/media/Movies/Example.Movie.2026.mkv",
        }


class FailingQbitlarrNotifier:
    async def poll_once(self):
        raise RuntimeError("temporary qBitlarr failure")


class DaemonTests(unittest.TestCase):
    def test_daemon_once_dispatches_qbitlarr_completion_to_babelarr_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = MediaWorkflowRuntime(root / "runtime")
            runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            sent_messages = []
            created_jobs = []
            started_jobs = []

            def fake_create_video(video_path, **kwargs):
                created_jobs.append((video_path, kwargs))
                return {"job": {"job_id": "job_123"}, "job_store": str(root / "jobs")}

            def fake_start(job_id, **kwargs):
                started_jobs.append((job_id, kwargs))
                return {"status": "started", "job": {"job_id": job_id}}

            async def send_message(target, message):
                sent_messages.append((target, message))

            async def completion_hook(event):
                dispatch_qbitlarr_completion(
                    runtime,
                    info_hash=event["info_hash"],
                    content_path=event["content_path"],
                    babelarr_job_create_video=fake_create_video,
                    babelarr_job_start=fake_start,
                )

            store = DownloadWatchStore(root / "watches.json")
            store.upsert_watch(
                info_hash="abc123",
                title="Example Movie",
                notification_target="telegram:123",
                requester_id="telegram:123",
            )
            notifier = DownloadCompletionNotifier(
                store=store,
                client=FakeQbitlarrClient(),
                send_message=send_message,
                completion_hook=completion_hook,
            )

            summary = asyncio.run(
                run_daemon_once(
                    qbitlarr_notifier=notifier,
                    run_babelarr_notifications_step=False,
                    run_runtime_dispatch=False,
                )
            )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(sent_messages[0][0], "telegram:123")
            self.assertEqual(created_jobs[0][0], "/media/Movies/Example.Movie.2026.mkv")
            self.assertEqual(created_jobs[0][1]["target_language"], "zh")
            self.assertEqual(started_jobs[0][0], "job_123")
            workflow = runtime.list_workflows()[0]
            self.assertEqual(workflow["tasks"][1]["babelarr"]["job_id"], "job_123")
            self.assertEqual(workflow["tasks"][1]["status"], "running")

    def test_daemon_lock_reports_already_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "mpilot-daemon.lock"
            handle = acquire_daemon_lock(lock_path)
            try:
                payload = run_daemon(
                    once=True,
                    lock_path=lock_path,
                    run_qbitlarr=False,
                    run_babelarr_notifications_step=False,
                    run_runtime_dispatch=False,
                )
            finally:
                release_daemon_lock(handle)

            self.assertEqual(payload["status"], "already_running")
            self.assertEqual(payload["lock_path"], str(lock_path))

    def test_daemon_once_isolates_step_failures_and_keeps_dispatching(self):
        dispatch_calls = []

        def fake_runtime_dispatcher(_runtime, **_kwargs):
            dispatch_calls.append("runtime")
            return {"status": "ok", "actions_claimed": 0}

        summary = asyncio.run(
            run_daemon_once(
                qbitlarr_notifier=FailingQbitlarrNotifier(),
                run_babelarr_notifications_step=False,
                runtime=MediaWorkflowRuntime(Path(tempfile.mkdtemp())),
                runtime_dispatcher=fake_runtime_dispatcher,
            )
        )

        self.assertEqual(summary["status"], "partial_failure")
        self.assertEqual(summary["errors"][0]["name"], "qbitlarr_downloads")
        self.assertEqual(dispatch_calls, ["runtime"])


if __name__ == "__main__":
    unittest.main()
