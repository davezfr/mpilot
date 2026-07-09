from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from media_workflow_runtime import MediaWorkflowRuntime
from media_workflow_runtime.dispatcher import dispatch_qbitlarr_completion, dispatch_ready_mst_actions
from babelarr.jobs import JobStore


class RuntimeDispatcherTests(unittest.TestCase):
    def test_qbitlarr_completion_dispatches_ready_subtitle_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=0.25,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            created_calls = []
            started_calls = []

            def fake_job_create_video(video_path, **kwargs):
                created_calls.append((video_path, kwargs))
                return {
                    "job_store": "/tmp/babelarr-jobs",
                    "job": {
                        "job_id": "job_123",
                        "status": "queued",
                    },
                }

            def fake_job_start(job_id, **kwargs):
                started_calls.append((job_id, kwargs))
                return {
                    "status": "started",
                    "job_store": "/tmp/babelarr-jobs",
                    "job": {
                        "job_id": job_id,
                        "status": "running",
                    },
                    "worker": {"pid": 1234},
                }

            summary = dispatch_qbitlarr_completion(
                runtime,
                info_hash="abc123",
                content_path="/mnt/media/Movies/Example.Movie.mkv",
                job_store_dir="/tmp/babelarr-jobs",
                notification_target="telegram:123",
                requester_id="telegram:123",
                notification_language="zh",
                backend="fake",
                mst_job_create_video=fake_job_create_video,
                mst_job_start=fake_job_start,
            )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["actions_claimed"], 1)
            self.assertEqual(created_calls[0][0], "/mnt/media/Movies/Example.Movie.mkv")
            self.assertEqual(
                created_calls[0][1],
                {
                    "imdb_id": "tt1234567",
                    "title": "Example Movie",
                    "media_type": "movie",
                    "source_language": "en",
                    "target_language": "zh",
                    "output_mode": "bilingual-ass",
                    "backend": "fake",
                    "model": None,
                    "job_store_dir": "/tmp/babelarr-jobs",
                    "allow_low_confidence_subtitle": False,
                    "allow_provider_fallback_language": False,
                },
            )
            self.assertEqual(started_calls[0][0], "job_123")
            self.assertEqual(started_calls[0][1]["notification_target"], "telegram:123")
            self.assertEqual(started_calls[0][1]["requester_id"], "telegram:123")
            self.assertEqual(started_calls[0][1]["title"], "Example Movie")
            self.assertEqual(started_calls[0][1]["notification_language"], "zh")
            self.assertEqual(started_calls[0][1]["runtime_store_dir"], str(runtime.root))
            self.assertEqual(started_calls[0][1]["runtime_workflow_id"], summary["dispatches"][0]["action"]["workflow_id"])
            self.assertEqual(started_calls[0][1]["runtime_task_id"], summary["dispatches"][0]["action"]["task_id"])

            workflow = runtime.workflow_for_qbitlarr_hash("abc123")
            subtitle_task = workflow["tasks"][1]
            self.assertEqual(subtitle_task["status"], "running")
            self.assertEqual(subtitle_task["babelarr"]["job_id"], "job_123")
            self.assertEqual(subtitle_task["babelarr"]["status"], "running")

    def test_qbitlarr_completion_without_subtitle_intent_only_records_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=0.25,
            )

            summary = dispatch_qbitlarr_completion(
                runtime,
                info_hash="abc123",
                content_path="/mnt/media/Movies/Example.Movie.mkv",
                mst_job_create_video=lambda *_args, **_kwargs: self.fail("should not create Babelarr job"),
                mst_job_start=lambda *_args, **_kwargs: self.fail("should not start Babelarr job"),
            )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["actions_claimed"], 0)
            self.assertEqual(summary["dispatches"], [])
            workflow = runtime.workflow_for_qbitlarr_hash("abc123")
            self.assertEqual(workflow["tasks"][0]["status"], "succeeded")

    def test_qbitlarr_completion_dispatches_global_next_ready_action_with_action_owner_notification(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            first = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:first",
                video_path="/mnt/media/Movies/First.mkv",
                title="First Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                notification_language="en",
                now="2026-06-11T10:00:00Z",
            )
            later = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:later",
                info_hash="later123",
                title="Later Movie",
                progress=0.25,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:01:00Z",
            )
            created_calls = []
            started_calls = []

            def fake_job_create_video(video_path, **kwargs):
                created_calls.append((video_path, kwargs))
                return {
                    "job_store": "/tmp/babelarr-jobs",
                    "job": {"job_id": "job_first", "status": "queued"},
                }

            def fake_job_start(job_id, **kwargs):
                started_calls.append((job_id, kwargs))
                return {
                    "status": "started",
                    "job_store": "/tmp/babelarr-jobs",
                    "job": {"job_id": job_id, "status": "running"},
                }

            summary = dispatch_qbitlarr_completion(
                runtime,
                info_hash="later123",
                content_path="/mnt/media/Movies/Later.mkv",
                job_store_dir="/tmp/babelarr-jobs",
                notification_target="telegram:later",
                requester_id="telegram:later",
                mst_job_create_video=fake_job_create_video,
                mst_job_start=fake_job_start,
            )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["actions_claimed"], 1)
            self.assertEqual(summary["dispatches"][0]["action"]["workflow_id"], first["workflow_id"])
            self.assertEqual(created_calls[0][0], "/mnt/media/Movies/First.mkv")
            self.assertEqual(started_calls[0][1]["notification_target"], "telegram:first")
            self.assertEqual(started_calls[0][1]["requester_id"], "telegram:first")
            self.assertEqual(started_calls[0][1]["title"], "First Movie")
            self.assertEqual(started_calls[0][1]["notification_language"], "en")
            self.assertEqual(runtime.workflow_summary(later["workflow_id"])["tasks"][1]["status"], "ready")

    def test_dispatch_ready_actions_attempts_one_queued_action_per_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="bad123",
                title="Bad Movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Bad.mkv",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:00:00Z",
            )
            good_workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="good123",
                title="Good Movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Good.mkv",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:01:00Z",
            )

            def fake_job_create_video(video_path, **kwargs):
                if "Bad.mkv" in video_path:
                    raise RuntimeError("create failed")
                return {
                    "job_store": "/tmp/babelarr-jobs",
                    "job": {"job_id": "job_good", "status": "queued"},
                }

            def fake_job_start(job_id, **kwargs):
                return {
                    "status": "started",
                    "job_store": "/tmp/babelarr-jobs",
                    "job": {"job_id": job_id, "status": "running"},
                }

            summary = dispatch_ready_mst_actions(
                runtime,
                mst_job_create_video=fake_job_create_video,
                mst_job_start=fake_job_start,
            )

            self.assertEqual(summary["status"], "partial_failure")
            self.assertEqual(summary["actions_claimed"], 1)
            self.assertEqual(summary["dispatches"], [])
            self.assertEqual(len(summary["errors"]), 1)
            self.assertEqual(summary["errors"][0]["error"]["message"], "create failed")
            failed = runtime.workflow_for_qbitlarr_hash("bad123")
            self.assertEqual(failed["tasks"][1]["status"], "failed")
            self.assertEqual(runtime.workflow_summary(good_workflow["workflow_id"])["tasks"][1]["status"], "ready")

            retry_summary = dispatch_ready_mst_actions(
                runtime,
                mst_job_create_video=fake_job_create_video,
                mst_job_start=fake_job_start,
            )

            self.assertEqual(retry_summary["status"], "ok")
            self.assertEqual(retry_summary["actions_claimed"], 1)
            self.assertEqual(len(retry_summary["dispatches"]), 1)
            succeeded = runtime.workflow_summary(good_workflow["workflow_id"])
            self.assertEqual(succeeded["tasks"][1]["status"], "running")

    def test_dispatch_ready_actions_reconciles_terminal_mst_job_before_claiming_next(self):
        with tempfile.TemporaryDirectory() as runtime_tmp, tempfile.TemporaryDirectory() as job_tmp:
            runtime = MediaWorkflowRuntime(Path(runtime_tmp))
            first_workflow = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:first",
                video_path="/mnt/media/Movies/First.mkv",
                title="First Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:00:00Z",
            )
            second_workflow = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:second",
                video_path="/mnt/media/Movies/Second.mkv",
                title="Second Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:01:00Z",
            )
            job_store = JobStore(Path(job_tmp))
            terminal_job = job_store.create(
                "translate-video",
                {"kind": "translate-video"},
                now="2026-06-11T10:02:00Z",
            )
            job_store.mark_running(terminal_job["job_id"], now="2026-06-11T10:03:00Z")
            job_store.mark_succeeded(
                terminal_job["job_id"],
                {"output": "/mnt/media/Movies/First.zh.ass"},
                now="2026-06-11T10:04:00Z",
            )

            first_action = runtime.claim_ready_mst_job_create_video_actions()[0]
            runtime.record_mst_job_created(
                workflow_id=first_action["workflow_id"],
                task_id=first_action["task_id"],
                mst_job_id=terminal_job["job_id"],
                now="2026-06-11T10:03:00Z",
            )
            runtime.record_mst_job_status(
                workflow_id=first_action["workflow_id"],
                task_id=first_action["task_id"],
                status="running",
                now="2026-06-11T10:03:30Z",
            )

            created_calls = []

            def fake_job_create_video(video_path, **kwargs):
                created_calls.append((video_path, kwargs))
                return {
                    "job_store": job_tmp,
                    "job": {"job_id": "job_second", "status": "queued"},
                }

            def fake_job_start(job_id, **kwargs):
                return {
                    "status": "started",
                    "job_store": job_tmp,
                    "job": {"job_id": job_id, "status": "running"},
                }

            summary = dispatch_ready_mst_actions(
                runtime,
                job_store_dir=job_tmp,
                mst_job_create_video=fake_job_create_video,
                mst_job_start=fake_job_start,
            )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["actions_claimed"], 1)
            self.assertEqual(summary["dispatches"][0]["action"]["workflow_id"], second_workflow["workflow_id"])
            self.assertEqual(created_calls[0][0], "/mnt/media/Movies/Second.mkv")
            self.assertEqual(runtime.workflow_summary(first_workflow["workflow_id"])["tasks"][0]["status"], "succeeded")
            self.assertEqual(runtime.workflow_summary(second_workflow["workflow_id"])["tasks"][0]["status"], "running")

    def test_dispatch_ready_actions_fails_stale_dispatching_task_without_mst_job(self):
        with tempfile.TemporaryDirectory() as runtime_tmp, tempfile.TemporaryDirectory() as job_tmp:
            runtime = MediaWorkflowRuntime(Path(runtime_tmp))
            stuck_workflow = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:first",
                video_path="/mnt/media/Movies/First.mkv",
                title="First Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:00:00Z",
            )
            second_workflow = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:second",
                video_path="/mnt/media/Movies/Second.mkv",
                title="Second Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:01:00Z",
            )
            # Claim the first task long ago and never create an Babelarr job for it,
            # simulating a dispatcher crash between claim and job creation.
            runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T10:02:00Z")

            created_calls = []

            def fake_job_create_video(video_path, **kwargs):
                created_calls.append(video_path)
                return {
                    "job_store": job_tmp,
                    "job": {"job_id": "job_second", "status": "queued"},
                }

            def fake_job_start(job_id, **kwargs):
                return {
                    "status": "started",
                    "job_store": job_tmp,
                    "job": {"job_id": job_id, "status": "running"},
                }

            summary = dispatch_ready_mst_actions(
                runtime,
                job_store_dir=job_tmp,
                mst_job_create_video=fake_job_create_video,
                mst_job_start=fake_job_start,
            )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["reconciled"][0]["workflow_id"], stuck_workflow["workflow_id"])
            self.assertEqual(summary["reconciled"][0]["status"], "failed")
            self.assertEqual(created_calls, ["/mnt/media/Movies/Second.mkv"])
            self.assertEqual(runtime.workflow_summary(stuck_workflow["workflow_id"])["tasks"][0]["status"], "failed")
            self.assertEqual(runtime.workflow_summary(second_workflow["workflow_id"])["tasks"][0]["status"], "running")

    def test_qbitlarr_completion_tolerates_untracked_hash_and_still_dispatches_local_video_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            # User asked for subtitles via local-video intent (no info_hash recorded),
            # then qBitlarr finishes the same torrent and fires the completion hook.
            local_workflow = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:123",
                video_path="/mnt/media/Movies/Untracked.mkv",
                title="Untracked Movie",
                source_language="en",
                target_language="zh",
                output_mode="single-srt",
            )
            created_calls = []
            started_calls = []

            def fake_job_create_video(video_path, **kwargs):
                created_calls.append(video_path)
                return {
                    "job_store": "/tmp/babelarr-jobs",
                    "job": {"job_id": "job_local", "status": "queued"},
                }

            def fake_job_start(job_id, **kwargs):
                started_calls.append(job_id)
                return {
                    "status": "started",
                    "job_store": "/tmp/babelarr-jobs",
                    "job": {"job_id": job_id, "status": "running"},
                }

            summary = dispatch_qbitlarr_completion(
                runtime,
                info_hash="never-tracked-hash",
                content_path="/mnt/media/Movies/Untracked.mkv",
                mst_job_create_video=fake_job_create_video,
                mst_job_start=fake_job_start,
            )

            self.assertEqual(summary["status"], "ok")
            self.assertEqual(summary["actions_claimed"], 1)
            self.assertEqual(summary["untracked_completion"]["info_hash"], "never-tracked-hash")
            self.assertEqual(summary["untracked_completion"]["content_path"], "/mnt/media/Movies/Untracked.mkv")
            self.assertEqual(created_calls, ["/mnt/media/Movies/Untracked.mkv"])
            self.assertEqual(started_calls, ["job_local"])
            self.assertEqual(
                runtime.workflow_summary(local_workflow["workflow_id"])["tasks"][0]["status"],
                "running",
            )


if __name__ == "__main__":
    unittest.main()
