from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from mpilot.runtime import RuntimeStoreError, mcp_server
from mpilot.subtitles.jobs import JobStore


class FakeFastMCP:
    def __init__(self, name, instructions=None):
        self.name = name
        self.instructions = instructions
        self.tool_names = []

    def tool(self):
        def decorator(func):
            self.tool_names.append(func.__name__)
            return func

        return decorator


class RuntimeMcpServerTests(unittest.TestCase):
    def test_create_mcp_server_registers_expected_runtime_tools(self):
        fake_fastmcp_module = types.ModuleType("mcp.server.fastmcp")
        fake_fastmcp_module.FastMCP = FakeFastMCP

        with patch.dict(
            sys.modules,
            {
                "mcp": types.ModuleType("mcp"),
                "mcp.server": types.ModuleType("mcp.server"),
                "mcp.server.fastmcp": fake_fastmcp_module,
            },
        ):
            server = mcp_server.create_mcp_server()

        self.assertEqual(server.name, "runtime")
        self.assertEqual(
            server.tool_names,
            [
                "record_acquisition_download",
                "record_acquisition_download_with_subtitle_intent",
                "attach_subtitle_intent",
                "record_local_video_subtitle_intent",
                "claim_ready_subtitle_job_create_video_actions",
                "record_subtitle_job_created",
                "record_subtitle_job_status",
                "queue_status",
                "workflow_show",
                "list_workflows",
            ],
        )

    def test_runtime_tools_share_runtime_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp))
            mcp_server.record_qbitlarr_download(
                runtime_store_dir=store_dir,
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )
            attached = mcp_server.attach_subtitle_intent(
                runtime_store_dir=store_dir,
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                notification_language="en",
            )
            task_id = attached["tasks"][1]["task_id"]
            workflow_id = attached["workflow_id"]

            actions = mcp_server.claim_ready_subtitle_job_create_video_actions(runtime_store_dir=store_dir)
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["action"], "subtitle_job_create_video")

            mcp_server.record_subtitle_job_created(
                runtime_store_dir=store_dir,
                workflow_id=workflow_id,
                task_id=task_id,
                subtitle_job_id="job_123",
            )
            summary = mcp_server.workflow_show(runtime_store_dir=store_dir, workflow_id=workflow_id)

            self.assertEqual(summary["tasks"][1]["babelarr"]["job_id"], "job_123")

    def test_combined_runtime_tool_records_download_and_subtitle_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            workflow = mcp_server.record_qbitlarr_download_with_subtitle_intent(
                runtime_store_dir=str(Path(tmp)),
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                notification_language="en",
            )

            self.assertEqual([task["task_type"] for task in workflow["tasks"]], ["download_media", "translate_subtitle"])
            self.assertEqual(workflow["tasks"][1]["status"], "waiting_for_dependency")
            self.assertEqual(workflow["tasks"][1]["subtitle"]["notification_language"], "en")

    def test_local_video_subtitle_intent_tool_records_ready_queue_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp))
            workflow = mcp_server.record_local_video_subtitle_intent(
                runtime_store_dir=store_dir,
                requester_id="telegram:123",
                video_path="/mnt/media/Movies/Example.Movie.mkv",
                title="Example Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                notification_language="en",
            )
            status = mcp_server.queue_status(runtime_store_dir=store_dir, requester_id="telegram:123")

            self.assertEqual([task["task_type"] for task in workflow["tasks"]], ["translate_subtitle"])
            self.assertEqual(workflow["tasks"][0]["status"], "ready")
            self.assertEqual(workflow["tasks"][0]["subtitle"]["notification_language"], "en")
            self.assertEqual(status["global"]["ready_count"], 1)
            self.assertEqual(status["requester_tasks"][0]["queue_position"], 1)

    def test_queue_status_tool_reconciles_terminal_babelarr_jobs_from_job_store(self):
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
            workflow = mcp_server.record_local_video_subtitle_intent(
                runtime_store_dir=runtime_tmp,
                requester_id="telegram:123",
                video_path="/mnt/media/Movies/Example.Movie.mkv",
                title="Example Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            task_id = workflow["tasks"][0]["task_id"]
            mcp_server.record_subtitle_job_created(
                runtime_store_dir=runtime_tmp,
                workflow_id=workflow["workflow_id"],
                task_id=task_id,
                subtitle_job_id=job["job_id"],
            )

            with patch.dict("os.environ", {"MPILOT_SUBTITLE_JOB_STORE_DIR": jobs_tmp}, clear=False):
                status = mcp_server.queue_status(runtime_store_dir=runtime_tmp, requester_id="telegram:123")
                workflow = mcp_server.workflow_show(runtime_store_dir=runtime_tmp, workflow_id=workflow["workflow_id"])

            self.assertEqual(status["global"]["active_count"], 0)
            self.assertEqual(status["global"]["total_open_count"], 0)
            self.assertEqual(status["requester_tasks"], [])
            self.assertEqual(workflow["status"], "succeeded")
            self.assertEqual(workflow["tasks"][0]["status"], "succeeded")
            self.assertEqual(workflow["tasks"][0]["babelarr"]["status"], "succeeded")

    def test_claim_ready_tool_reconciles_terminal_babelarr_jobs_before_blocking(self):
        with tempfile.TemporaryDirectory() as runtime_tmp, tempfile.TemporaryDirectory() as jobs_tmp:
            job_store = JobStore(Path(jobs_tmp))
            first_job = job_store.create(
                "translate-video",
                {
                    "video_path": "/mnt/media/TV/Show.S09E03.mkv",
                    "source_language": "en",
                    "target_language": "zh",
                    "output_mode": "bilingual-ass",
                },
            )
            job_store.mark_running(first_job["job_id"], now="2026-06-22T13:19:00Z")
            job_store.mark_succeeded(
                first_job["job_id"],
                {"input": "/mnt/media/TV/Show.S09E03.mkv", "output": "/mnt/media/TV/Show.S09E03.zh.ass"},
                now="2026-06-22T13:20:00Z",
            )
            first = mcp_server.record_local_video_subtitle_intent(
                runtime_store_dir=runtime_tmp,
                requester_id="telegram:123",
                video_path="/mnt/media/TV/Show.S09E03.mkv",
                title="Show S09E03",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            first_task_id = first["tasks"][0]["task_id"]
            mcp_server.claim_ready_subtitle_job_create_video_actions(runtime_store_dir=runtime_tmp)
            mcp_server.record_subtitle_job_created(
                runtime_store_dir=runtime_tmp,
                workflow_id=first["workflow_id"],
                task_id=first_task_id,
                subtitle_job_id=first_job["job_id"],
            )
            second = mcp_server.record_local_video_subtitle_intent(
                runtime_store_dir=runtime_tmp,
                requester_id="telegram:123",
                video_path="/mnt/media/TV/Show.S09E04.mkv",
                title="Show S09E04",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )

            with patch.dict("os.environ", {"MPILOT_SUBTITLE_JOB_STORE_DIR": jobs_tmp}, clear=False):
                actions = mcp_server.claim_ready_subtitle_job_create_video_actions(runtime_store_dir=runtime_tmp)
                first_summary = mcp_server.workflow_show(runtime_store_dir=runtime_tmp, workflow_id=first["workflow_id"])

            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["action"], "subtitle_job_create_video")
            self.assertEqual(actions[0]["workflow_id"], second["workflow_id"])
            self.assertEqual(actions[0]["arguments"]["title"], "Show S09E04")
            self.assertEqual(first_summary["tasks"][0]["status"], "succeeded")

    def test_runtime_tools_reject_invalid_output_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeStoreError):
                mcp_server.record_qbitlarr_download_with_subtitle_intent(
                    runtime_store_dir=str(Path(tmp)),
                    requester_id="telegram:123",
                    info_hash="abc123",
                    source_language="en",
                    target_language="zh",
                    output_mode="invalid-mode",
                )

    def test_record_local_video_subtitle_intent_maps_container_path_via_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp))
            env = {
                "MWR_CONTENT_PATH_PREFIX": "/media",
                "MWR_LOCAL_CONTENT_PATH_PREFIX": "/mnt/media",
            }
            with patch.dict("os.environ", env, clear=False):
                workflow = mcp_server.record_local_video_subtitle_intent(
                    runtime_store_dir=store_dir,
                    requester_id="telegram:123",
                    video_path="/media/Movies - HD/Toy Story 2 (1999) [1080p]",
                    title="Toy Story 2",
                    source_language="en",
                    target_language="zh",
                    output_mode="single-srt",
                )

            self.assertEqual(
                workflow["artifacts"]["video_path"],
                "/mnt/media/Movies - HD/Toy Story 2 (1999) [1080p]",
            )

    def test_record_qbitlarr_download_maps_container_content_path_via_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = str(Path(tmp))
            env = {
                "MWR_CONTENT_PATH_PREFIX": "/media",
                "MWR_LOCAL_CONTENT_PATH_PREFIX": "/mnt/media",
            }
            with patch.dict("os.environ", env, clear=False):
                workflow = mcp_server.record_qbitlarr_download(
                    runtime_store_dir=store_dir,
                    requester_id="telegram:123",
                    info_hash="abc123",
                    title="Toy Story 2",
                    progress=1.0,
                    content_path="/media/Movies - HD/Toy Story 2 (1999) [1080p]",
                )

            self.assertEqual(
                workflow["artifacts"]["video_path"],
                "/mnt/media/Movies - HD/Toy Story 2 (1999) [1080p]",
            )


if __name__ == "__main__":
    unittest.main()
