from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from mpilot.subtitles.jobs import JobStore
from mpilot.runtime import cli as runtime_cli


class RuntimeCliTests(unittest.TestCase):
    def test_default_runtime_store_dir_prefers_babelarr_env(self):
        with patch.dict(
            "os.environ",
            {
                "BABELARR_RUNTIME_STORE_DIR": "/tmp/babelarr-runtime",
                "MWR_STORE_DIR": "/tmp/legacy-runtime",
            },
            clear=False,
        ):
            self.assertEqual(runtime_cli.default_runtime_store_dir(), Path("/tmp/babelarr-runtime"))

    def test_record_attach_claim_and_track_mst_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp)

            recorded = runtime_cli.summary_from_argv(
                [
                    "record-acquisition-download",
                    "--runtime-store-dir",
                    str(store_dir),
                    "--requester-id",
                    "telegram:123",
                    "--info-hash",
                    "abc123",
                    "--title",
                    "Example Movie",
                    "--imdb-id",
                    "tt1234567",
                    "--media-type",
                    "movie",
                    "--progress",
                    "1.0",
                    "--content-path",
                    "/mnt/media/Movies/Example.Movie.mkv",
                ]
            )
            self.assertEqual(recorded["status"], "ok")

            attached = runtime_cli.summary_from_argv(
                [
                    "attach-subtitle-intent",
                    "--runtime-store-dir",
                    str(store_dir),
                    "--requester-id",
                    "telegram:123",
                    "--source-language",
                    "en",
                    "--target-language",
                    "zh",
                    "--output-mode",
                    "bilingual-ass",
                    "--notification-language",
                    "en",
                ]
            )
            workflow_id = attached["workflow"]["workflow_id"]
            task_id = attached["workflow"]["tasks"][1]["task_id"]

            claimed = runtime_cli.summary_from_argv(
                [
                    "claim-ready-subtitle-job-actions",
                    "--runtime-store-dir",
                    str(store_dir),
                ]
            )
            self.assertEqual(claimed["status"], "ok")
            self.assertEqual(len(claimed["actions"]), 1)
            self.assertEqual(claimed["actions"][0]["workflow_id"], workflow_id)
            self.assertEqual(claimed["actions"][0]["notification_language"], "en")
            self.assertNotIn("notification_language", claimed["actions"][0]["arguments"])

            created = runtime_cli.summary_from_argv(
                [
                    "record-subtitle-job-created",
                    "--runtime-store-dir",
                    str(store_dir),
                    "--workflow-id",
                    workflow_id,
                    "--task-id",
                    task_id,
                    "--subtitle-job-id",
                    "job_123",
                ]
            )
            self.assertEqual(created["workflow"]["tasks"][1]["status"], "queued")

            updated = runtime_cli.summary_from_argv(
                [
                    "record-subtitle-job-status",
                    "--runtime-store-dir",
                    str(store_dir),
                    "--workflow-id",
                    workflow_id,
                    "--task-id",
                    task_id,
                    "--status",
                    "running",
                    "--status-detail-json",
                    '{"stage":"translating"}',
                ]
            )
            self.assertEqual(updated["workflow"]["tasks"][1]["babelarr"]["status_detail"], {"stage": "translating"})

            shown = runtime_cli.summary_from_argv(
                [
                    "workflow-show",
                    "--runtime-store-dir",
                    str(store_dir),
                    "--workflow-id",
                    workflow_id,
                ]
            )
            self.assertEqual(shown["workflow"]["workflow_id"], workflow_id)
            self.assertEqual(shown["workflow"]["tasks"][1]["babelarr"]["job_id"], "job_123")

    def test_record_download_with_subtitle_intent_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = runtime_cli.summary_from_argv(
                [
                    "record-acquisition-download-with-subtitle-intent",
                    "--runtime-store-dir",
                    tmp,
                    "--requester-id",
                    "telegram:123",
                    "--info-hash",
                    "abc123",
                    "--title",
                    "Example Movie",
                    "--source-language",
                    "en",
                    "--target-language",
                    "zh",
                    "--output-mode",
                    "bilingual-ass",
                ]
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual([task["task_type"] for task in result["workflow"]["tasks"]], ["download_media", "translate_subtitle"])

    def test_record_local_video_subtitle_intent_and_queue_status_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorded = runtime_cli.summary_from_argv(
                [
                    "record-local-video-subtitle-intent",
                    "--runtime-store-dir",
                    tmp,
                    "--requester-id",
                    "telegram:123",
                    "--video-path",
                    "/mnt/media/Movies/Example.Movie.mkv",
                    "--title",
                    "Example Movie",
                    "--source-language",
                    "en",
                    "--target-language",
                    "zh",
                    "--output-mode",
                    "bilingual-ass",
                ]
            )
            queue = runtime_cli.summary_from_argv(
                [
                    "queue-status",
                    "--runtime-store-dir",
                    tmp,
                    "--requester-id",
                    "telegram:123",
                ]
            )

            self.assertEqual(recorded["status"], "ok")
            self.assertEqual(recorded["workflow"]["tasks"][0]["status"], "ready")
            self.assertEqual(queue["status"], "ok")
            self.assertEqual(queue["queue"]["global"]["ready_count"], 1)
            self.assertEqual(queue["queue"]["requester_tasks"][0]["queue_position"], 1)

    def test_queue_status_reconciles_terminal_babelarr_jobs_from_job_store(self):
        with tempfile.TemporaryDirectory() as runtime_tmp, tempfile.TemporaryDirectory() as jobs_tmp:
            job_store = JobStore(Path(jobs_tmp))
            job = job_store.create(
                "translate-video",
                {
                    "video_path": "/mnt/media/Movies/Example.Movie.mkv",
                    "source_language": "en",
                    "target_language": "zh",
                    "output_mode": "bilingual-ass",
                },
            )
            job_store.mark_running(job["job_id"], now="2026-06-22T13:19:00Z")
            job_store.mark_succeeded(
                job["job_id"],
                {"input": "/mnt/media/Movies/Example.Movie.mkv", "output": "/mnt/media/Movies/Example.Movie.zh.ass"},
                now="2026-06-22T13:20:00Z",
            )
            recorded = runtime_cli.summary_from_argv(
                [
                    "record-local-video-subtitle-intent",
                    "--runtime-store-dir",
                    runtime_tmp,
                    "--requester-id",
                    "telegram:123",
                    "--video-path",
                    "/mnt/media/Movies/Example.Movie.mkv",
                    "--title",
                    "Example Movie",
                    "--source-language",
                    "en",
                    "--target-language",
                    "zh",
                    "--output-mode",
                    "bilingual-ass",
                ]
            )
            task_id = recorded["workflow"]["tasks"][0]["task_id"]
            workflow_id = recorded["workflow"]["workflow_id"]
            runtime_cli.summary_from_argv(
                [
                    "record-subtitle-job-created",
                    "--runtime-store-dir",
                    runtime_tmp,
                    "--workflow-id",
                    workflow_id,
                    "--task-id",
                    task_id,
                    "--subtitle-job-id",
                    job["job_id"],
                ]
            )

            with patch.dict("os.environ", {"BABELARR_JOB_STORE_DIR": jobs_tmp}, clear=False):
                queue = runtime_cli.summary_from_argv(
                    [
                        "queue-status",
                        "--runtime-store-dir",
                        runtime_tmp,
                        "--requester-id",
                        "telegram:123",
                    ]
                )
                workflow = runtime_cli.summary_from_argv(
                    [
                        "workflow-show",
                        "--runtime-store-dir",
                        runtime_tmp,
                        "--workflow-id",
                        workflow_id,
                    ]
                )

            self.assertEqual(queue["status"], "ok")
            self.assertEqual(queue["queue"]["global"]["active_count"], 0)
            self.assertEqual(queue["queue"]["global"]["total_open_count"], 0)
            self.assertEqual(queue["queue"]["requester_tasks"], [])
            self.assertEqual(workflow["workflow"]["status"], "succeeded")
            self.assertEqual(workflow["workflow"]["tasks"][0]["status"], "succeeded")
            self.assertEqual(workflow["workflow"]["tasks"][0]["babelarr"]["status"], "succeeded")

    def test_handle_qbitlarr_completion_dispatches_from_event_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                runtime_cli,
                "dispatch_qbitlarr_completion",
                return_value={"status": "ok", "actions_claimed": 1, "dispatches": []},
            ) as dispatch:
                result = runtime_cli.summary_from_argv(
                    [
                        "handle-acquisition-completion",
                        "--runtime-store-dir",
                        tmp,
                        "--event-json",
                        '{"info_hash":"abc123","content_path":"/media/Movie.mkv","requester_id":"telegram:123","notification_target":"telegram:123"}',
                        "--job-store-dir",
                        "/tmp/babelarr-jobs",
                        "--backend",
                        "fake",
                        "--notification-language",
                        "zh",
                    ]
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(dispatch.call_args.args[1:], ())
            self.assertEqual(dispatch.call_args.kwargs["info_hash"], "abc123")
            self.assertEqual(dispatch.call_args.kwargs["content_path"], "/media/Movie.mkv")
            self.assertEqual(dispatch.call_args.kwargs["requester_id"], "telegram:123")
            self.assertEqual(dispatch.call_args.kwargs["notification_target"], "telegram:123")
            self.assertEqual(dispatch.call_args.kwargs["notification_language"], "zh")
            self.assertEqual(dispatch.call_args.kwargs["job_store_dir"], "/tmp/babelarr-jobs")
            self.assertEqual(dispatch.call_args.kwargs["backend"], "fake")

    def test_handle_qbitlarr_completion_maps_acquisition_path_for_mst(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                runtime_cli,
                "dispatch_qbitlarr_completion",
                return_value={"status": "ok", "actions_claimed": 1, "dispatches": []},
            ) as dispatch:
                result = runtime_cli.summary_from_argv(
                    [
                        "handle-acquisition-completion",
                        "--runtime-store-dir",
                        tmp,
                        "--event-json",
                        '{"info_hash":"abc123","content_path":"/media/Movies - HD/Movie.mkv"}',
                        "--content-path-prefix",
                        "/media",
                        "--local-content-path-prefix",
                        "/mnt/media",
                    ]
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                dispatch.call_args.kwargs["content_path"],
                "/mnt/media/Movies - HD/Movie.mkv",
            )

    def test_handle_qbitlarr_removed_event_clears_workflow_without_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp)
            recorded = runtime_cli.summary_from_argv(
                [
                    "record-acquisition-download-with-subtitle-intent",
                    "--runtime-store-dir",
                    str(store_dir),
                    "--requester-id",
                    "telegram:123",
                    "--info-hash",
                    "abc123",
                    "--title",
                    "Example Movie",
                    "--source-language",
                    "en",
                    "--target-language",
                    "zh",
                    "--output-mode",
                    "bilingual-ass",
                ]
            )
            workflow_id = recorded["workflow"]["workflow_id"]

            with patch.object(runtime_cli, "dispatch_qbitlarr_completion") as dispatch:
                result = runtime_cli.summary_from_argv(
                    [
                        "handle-acquisition-completion",
                        "--runtime-store-dir",
                        str(store_dir),
                        "--event-json",
                        '{"event":"download_removed","info_hash":"abc123","error":{"type":"AcquisitionApiError","message":"Download not found"}}',
                    ]
                )

            dispatch.assert_not_called()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["event"], "download_removed")
            self.assertEqual(result["workflow_clear"]["status"], "cleared")
            self.assertEqual(result["workflow_clear"]["workflow_id"], workflow_id)
            listed = runtime_cli.summary_from_argv(
                [
                    "workflow-list",
                    "--runtime-store-dir",
                    str(store_dir),
                ]
            )
            self.assertEqual(listed["workflows"], [])

    def test_handle_qbitlarr_completion_errors_in_tty_without_event_or_required_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sys.stdin, "isatty", return_value=True):
                result = runtime_cli.summary_from_argv(
                    [
                        "handle-acquisition-completion",
                        "--runtime-store-dir",
                        tmp,
                    ]
                )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["type"], "RuntimeStoreError")
        self.assertIn("completion event JSON is required", result["error"]["message"])

    def test_summary_returns_structured_error_for_missing_workflow(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = runtime_cli.summary_from_argv(
                [
                    "workflow-show",
                    "--runtime-store-dir",
                    tmp,
                    "--workflow-id",
                    "workflow_missing",
                ]
            )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["type"], "RuntimeStoreError")

    def test_record_local_video_subtitle_intent_maps_container_path_to_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = runtime_cli.summary_from_argv(
                [
                    "record-local-video-subtitle-intent",
                    "--runtime-store-dir",
                    tmp,
                    "--requester-id",
                    "telegram:123",
                    "--video-path",
                    "/media/Movies - HD/Toy Story 2 (1999) [1080p]",
                    "--source-language",
                    "en",
                    "--target-language",
                    "zh",
                    "--output-mode",
                    "single-srt",
                    "--content-path-prefix",
                    "/media",
                    "--local-content-path-prefix",
                    "/mnt/media",
                ]
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                result["workflow"]["artifacts"]["video_path"],
                "/mnt/media/Movies - HD/Toy Story 2 (1999) [1080p]",
            )

    def test_record_qbitlarr_download_maps_container_content_path_to_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = runtime_cli.summary_from_argv(
                [
                    "record-acquisition-download",
                    "--runtime-store-dir",
                    tmp,
                    "--requester-id",
                    "telegram:123",
                    "--info-hash",
                    "abc123",
                    "--title",
                    "Toy Story 2",
                    "--progress",
                    "1.0",
                    "--content-path",
                    "/media/Movies - HD/Toy Story 2 (1999) [1080p]",
                    "--content-path-prefix",
                    "/media",
                    "--local-content-path-prefix",
                    "/mnt/media",
                ]
            )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                result["workflow"]["artifacts"]["video_path"],
                "/mnt/media/Movies - HD/Toy Story 2 (1999) [1080p]",
            )
            self.assertEqual(
                result["workflow"]["tasks"][0]["qbitlarr"]["content_path"],
                "/mnt/media/Movies - HD/Toy Story 2 (1999) [1080p]",
            )


if __name__ == "__main__":
    unittest.main()
