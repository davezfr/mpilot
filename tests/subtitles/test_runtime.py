from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mpilot.runtime import (
    MediaWorkflowRuntime,
    RuntimeStoreError,
)


class MediaWorkflowRuntimeTests(unittest.TestCase):
    def test_list_workflows_quarantines_corrupt_state_and_keeps_valid_workflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = MediaWorkflowRuntime(root)
            valid = runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
            )
            corrupt_path = root / "workflow_corrupt.json"
            corrupt_path.write_text("{broken", encoding="utf-8")

            workflows = runtime.list_workflows()

            self.assertEqual([workflow["workflow_id"] for workflow in workflows], [valid["workflow_id"]])
            self.assertFalse(corrupt_path.exists())
            self.assertTrue((root / "workflow_corrupt.json.corrupt").exists())

    def test_workflow_summary_quarantines_mismatched_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = MediaWorkflowRuntime(root)
            corrupt_path = root / "workflow_corrupt.json"
            corrupt_path.write_text('{"workflow_id":"workflow_other"}', encoding="utf-8")

            with self.assertRaisesRegex(RuntimeStoreError, "corrupt workflow state quarantined"):
                runtime.workflow_summary("workflow_corrupt")

            self.assertTrue((root / "workflow_corrupt.json.corrupt").exists())

    def test_subtitle_request_attaches_to_single_active_download_and_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            workflow = runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=0.25,
            )

            attached = runtime.attach_subtitle_intent_to_current_download(
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )

            self.assertEqual(attached["workflow_id"], workflow["workflow_id"])
            subtitle_task = attached["tasks"][1]
            self.assertEqual(subtitle_task["task_type"], "translate_subtitle")
            self.assertEqual(subtitle_task["status"], "waiting_for_dependency")
            self.assertEqual(subtitle_task["depends_on"], [workflow["tasks"][0]["task_id"]])
            self.assertEqual(subtitle_task["subtitle"]["target_language"], "zh")

    def test_record_download_with_subtitle_intent_is_one_idempotent_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))

            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
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
            repeated = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=0.4,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                notification_language="en",
            )

            self.assertEqual(workflow["workflow_id"], repeated["workflow_id"])
            self.assertEqual([task["task_type"] for task in repeated["tasks"]], ["download_media", "translate_subtitle"])
            self.assertEqual(repeated["tasks"][0]["qbitlarr"]["progress"], 0.4)
            self.assertEqual(repeated["tasks"][1]["status"], "waiting_for_dependency")
            self.assertEqual(repeated["tasks"][1]["subtitle"]["output_mode"], "bilingual-ass")
            self.assertEqual(repeated["tasks"][1]["subtitle"]["notification_language"], "en")

            runtime.mark_qbitlarr_download_complete(
                info_hash="abc123",
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )
            actions = runtime.claim_ready_mst_job_create_video_actions()
            self.assertEqual(actions[0]["notification_language"], "en")
            self.assertNotIn("notification_language", actions[0]["arguments"])

    def test_same_hash_different_requesters_keep_independent_subtitle_intents(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))

            first = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:first",
                notification_target="telegram:chat-first",
                info_hash="abc123",
                title="Shared Movie",
                progress=0.25,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            second = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:second",
                notification_target="telegram:chat-second",
                info_hash="abc123",
                title="Shared Movie",
                progress=0.25,
                source_language="fr",
                target_language="en",
                output_mode="single-srt",
            )

            self.assertNotEqual(first["workflow_id"], second["workflow_id"])
            workflows = {item["requester_id"]: item for item in runtime.list_workflows()}
            self.assertEqual(set(workflows), {"telegram:first", "telegram:second"})
            self.assertEqual(workflows["telegram:first"]["tasks"][1]["subtitle"]["target_language"], "zh")
            self.assertEqual(workflows["telegram:second"]["tasks"][1]["subtitle"]["source_language"], "fr")

            actions = runtime.mark_qbitlarr_download_complete(
                info_hash="abc123",
                content_path="/mnt/media/Movies/Shared.Movie.mkv",
            )

            self.assertEqual({action["requester_id"] for action in actions}, {"telegram:first", "telegram:second"})
            self.assertEqual(
                {action["notification_target"] for action in actions},
                {"telegram:chat-first", "telegram:chat-second"},
            )
            first_after = runtime.workflow_summary(first["workflow_id"])
            second_after = runtime.workflow_summary(second["workflow_id"])
            self.assertEqual(first_after["tasks"][1]["status"], "ready")
            self.assertEqual(second_after["tasks"][1]["status"], "ready")
            self.assertEqual(second_after["tasks"][1]["subtitle"]["output_mode"], "single-srt")

    def test_workflow_summary_rejects_path_traversal_workflow_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "runtime"
            root.mkdir()
            (Path(tmp) / "outside.json").write_text(
                json.dumps({"workflow_id": "outside", "requester_id": "sensitive-requester", "tasks": []}),
                encoding="utf-8",
            )
            runtime = MediaWorkflowRuntime(root)

            with self.assertRaisesRegex(RuntimeStoreError, "invalid workflow_id"):
                runtime.workflow_summary("../outside")

    def test_workflow_summary_rejects_absolute_and_dotted_workflow_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))

            for workflow_id in ("/tmp/outside", "workflow_123/child", "workflow_123\\child", ".", "..", "workflow_.."):
                with self.subTest(workflow_id=workflow_id):
                    with self.assertRaisesRegex(RuntimeStoreError, "invalid workflow_id"):
                        runtime.workflow_summary(workflow_id)

    def test_incomplete_download_path_does_not_release_subtitle_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))

            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=0.25,
                content_path="/mnt/media/Movies/Example.Movie.part.mkv",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            actions = runtime.ready_mst_job_create_video_actions()

            self.assertEqual(workflow.get("artifacts"), {})
            self.assertEqual(workflow["tasks"][0]["status"], "running")
            self.assertNotIn("content_path", workflow["tasks"][0]["qbitlarr"])
            self.assertEqual(workflow["tasks"][1]["status"], "waiting_for_dependency")
            self.assertEqual(actions, [])

    def test_known_video_path_does_not_release_subtitle_before_download_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))

            workflow = runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=0.25,
            )
            workflow.setdefault("artifacts", {})["video_path"] = "/mnt/media/Movies/Example.Movie.mkv"
            runtime._save(workflow)

            attached = runtime.attach_subtitle_intent_to_current_download(
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            actions = runtime.claim_ready_mst_job_create_video_actions()

            self.assertEqual(attached["tasks"][0]["status"], "running")
            self.assertEqual(attached["tasks"][1]["status"], "waiting_for_dependency")
            self.assertEqual(actions, [])

    def test_restarted_same_hash_download_replaces_stale_subtitle_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))

            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.Movie.mkv",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            old_task_id = workflow["tasks"][1]["task_id"]
            runtime.claim_ready_mst_job_create_video_actions()
            runtime.record_mst_job_created(
                workflow_id=workflow["workflow_id"],
                task_id=old_task_id,
                mst_job_id="job_old",
            )
            runtime.record_mst_job_status(
                workflow_id=workflow["workflow_id"],
                task_id=old_task_id,
                status="running",
                status_detail={"stage": "translating"},
            )

            restarted = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=0.1,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )

            self.assertNotIn("video_path", restarted.get("artifacts") or {})
            self.assertEqual([task["task_type"] for task in restarted["tasks"]], ["download_media", "translate_subtitle"])
            self.assertEqual(restarted["tasks"][0]["status"], "running")
            self.assertEqual(restarted["tasks"][1]["status"], "waiting_for_dependency")
            self.assertNotEqual(restarted["tasks"][1]["task_id"], old_task_id)
            self.assertNotIn("babelarr", restarted["tasks"][1])
            self.assertNotIn("dispatch", restarted["tasks"][1])

            runtime.mark_qbitlarr_download_complete(
                info_hash="abc123",
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )
            actions = runtime.claim_ready_mst_job_create_video_actions()

            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["task_id"], restarted["tasks"][1]["task_id"])

    def test_completed_download_replaces_premature_running_subtitle_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))

            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=0.25,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            old_task_id = workflow["tasks"][1]["task_id"]
            workflow["tasks"][1]["status"] = "running"
            workflow["tasks"][1]["babelarr"] = {"job_id": "job_old", "status": "running"}
            workflow["tasks"][1]["dispatch"] = {"action": "mst_job_create_video", "claimed_at": "2026-06-11T11:00:00Z"}
            runtime._save(workflow)

            actions = runtime.mark_qbitlarr_download_complete(
                info_hash="abc123",
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )
            updated = runtime.workflow_for_qbitlarr_hash("abc123")

            self.assertEqual(len(actions), 1)
            self.assertNotEqual(actions[0]["task_id"], old_task_id)
            self.assertEqual(updated["tasks"][1]["status"], "ready")
            self.assertEqual(updated["tasks"][1]["subtitle"]["output_mode"], "bilingual-ass")
            self.assertNotIn("babelarr", updated["tasks"][1])
            self.assertNotIn("dispatch", updated["tasks"][1])

    def test_replayed_completion_replaces_stale_running_subtitle_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))

            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.Movie.mkv",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            old_task_id = workflow["tasks"][1]["task_id"]
            workflow["tasks"][0]["updated_at"] = "2026-06-11T12:44:01Z"
            workflow["tasks"][1]["status"] = "running"
            workflow["tasks"][1]["updated_at"] = "2026-06-11T11:34:19Z"
            workflow["tasks"][1]["babelarr"] = {"job_id": "job_old", "status": "running"}
            workflow["tasks"][1]["dispatch"] = {"action": "mst_job_create_video", "claimed_at": "2026-06-11T11:34:19Z"}
            runtime._save(workflow)

            actions = runtime.mark_qbitlarr_download_complete(
                info_hash="abc123",
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )

            self.assertEqual(len(actions), 1)
            self.assertNotEqual(actions[0]["task_id"], old_task_id)
            self.assertEqual(actions[0]["arguments"]["video_path"], "/mnt/media/Movies/Example.Movie.mkv")

    def test_removed_qbitlarr_download_clears_workflow_and_downstream_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
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

            summary = runtime.clear_qbitlarr_download_workflow(
                info_hash="abc123",
                reason="download_removed",
                error={"type": "AcquisitionApiError", "message": "Download not found"},
            )

            self.assertEqual(summary["status"], "cleared")
            self.assertEqual(summary["workflow_id"], workflow["workflow_id"])
            self.assertEqual(summary["tasks_cleared"], ["download_media", "translate_subtitle"])
            self.assertEqual(runtime.list_workflows(), [])
            with self.assertRaisesRegex(RuntimeStoreError, "download task not found"):
                runtime.workflow_for_qbitlarr_hash("abc123")

    def test_removed_shared_download_clears_all_requester_workflows(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            first = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:first",
                info_hash="abc123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            second = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:second",
                info_hash="ABC123",
                source_language="fr",
                target_language="en",
                output_mode="single-srt",
            )

            summary = runtime.clear_qbitlarr_download_workflow(info_hash="abc123")

            self.assertEqual(summary["status"], "cleared")
            self.assertEqual(summary["workflows_cleared"], 2)
            self.assertEqual(set(summary["workflow_ids"]), {first["workflow_id"], second["workflow_id"]})
            self.assertEqual(runtime.list_workflows(), [])

    def test_combined_download_with_subtitle_intent_attaches_to_recorded_hash_when_other_downloads_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="other123",
                title="Other Movie",
                progress=0.2,
            )

            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Target Movie",
                progress=0.25,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )

            self.assertEqual(workflow["tasks"][0]["qbitlarr"]["info_hash"], "abc123")
            self.assertEqual([task["task_type"] for task in workflow["tasks"]], ["download_media", "translate_subtitle"])
            other = runtime.workflow_for_qbitlarr_hash("other123")
            self.assertEqual([task["task_type"] for task in other["tasks"]], ["download_media"])

    def test_claim_ready_actions_waits_for_cross_process_store_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = MediaWorkflowRuntime(root)
            runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.Movie.mkv",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            ready_file = root / "lock-held"
            script = (
                "import fcntl, pathlib, time; "
                "root=pathlib.Path(%r); "
                "root.mkdir(parents=True, exist_ok=True); "
                "lock=(root/'.runtime.lock').open('a+'); "
                "fcntl.flock(lock, fcntl.LOCK_EX); "
                "(root/'lock-held').write_text('1'); "
                "time.sleep(0.35); "
                "fcntl.flock(lock, fcntl.LOCK_UN)"
            ) % str(root)
            process = subprocess.Popen([sys.executable, "-c", script])
            try:
                deadline = time.monotonic() + 2.0
                while not ready_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready_file.exists())

                started = time.monotonic()
                actions = runtime.claim_ready_mst_job_create_video_actions()
                elapsed = time.monotonic() - started
            finally:
                process.wait(timeout=5)

            self.assertGreaterEqual(elapsed, 0.25)
            self.assertEqual(len(actions), 1)

    def test_completed_download_releases_ready_babelarr_direct_video_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=0.1,
            )
            runtime.attach_subtitle_intent_to_current_download(
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )

            actions = runtime.mark_qbitlarr_download_complete(
                info_hash="abc123",
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )

            self.assertEqual(len(actions), 1)
            action = actions[0]
            self.assertEqual(action["action"], "babelarr_job_create_video")
            self.assertEqual(
                action["arguments"],
                {
                    "video_path": "/mnt/media/Movies/Example.Movie.mkv",
                    "imdb_id": "tt1234567",
                    "title": "Example Movie",
                    "media_type": "movie",
                    "source_language": "en",
                    "target_language": "zh",
                    "output_mode": "bilingual-ass",
                },
            )

            workflow = runtime.workflow_for_qbitlarr_hash("abc123")
            self.assertEqual(workflow["tasks"][0]["status"], "succeeded")
            self.assertEqual(workflow["tasks"][1]["status"], "ready")

    def test_qbitlarr_progress_update_with_content_path_releases_waiting_subtitle_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=0.1,
            )
            runtime.attach_subtitle_intent_to_current_download(
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )

            updated = runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )
            actions = runtime.ready_mst_job_create_video_actions()

            self.assertEqual(updated["tasks"][0]["status"], "succeeded")
            self.assertEqual(updated["tasks"][1]["status"], "ready")
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["arguments"]["video_path"], "/mnt/media/Movies/Example.Movie.mkv")

    def test_subtitle_request_after_download_completion_is_ready_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )

            runtime.attach_subtitle_intent_to_current_download(
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="single-srt",
            )
            actions = runtime.ready_mst_job_create_video_actions()

            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["arguments"]["video_path"], "/mnt/media/Movies/Example.Movie.mkv")
            self.assertEqual(actions[0]["arguments"]["output_mode"], "single-srt")

    def test_claim_ready_action_marks_task_dispatching_and_prevents_duplicate_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            workflow = runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )
            runtime.attach_subtitle_intent_to_current_download(
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )

            actions = runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T12:00:00Z")
            duplicate_actions = runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T12:00:01Z")

            self.assertEqual(len(actions), 1)
            self.assertEqual(duplicate_actions, [])
            claimed = runtime.workflow_summary(workflow["workflow_id"])
            subtitle_task = claimed["tasks"][1]
            self.assertEqual(subtitle_task["status"], "dispatching")
            self.assertEqual(subtitle_task["dispatch"]["claimed_at"], "2026-06-11T12:00:00Z")

    def test_global_queue_claims_one_ready_task_and_skips_waiting_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            waiting = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:111",
                info_hash="waiting123",
                title="Waiting Movie",
                progress=0.1,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:00:00Z",
            )
            ready_one = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:222",
                video_path="/mnt/media/Movies/Ready.One.mkv",
                title="Ready One",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:01:00Z",
            )
            ready_two = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:333",
                video_path="/mnt/media/Movies/Ready.Two.mkv",
                title="Ready Two",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:02:00Z",
            )

            actions = runtime.claim_ready_mst_job_create_video_actions(limit=5, now="2026-06-11T10:03:00Z")

            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["workflow_id"], ready_one["workflow_id"])
            self.assertEqual(actions[0]["arguments"]["video_path"], "/mnt/media/Movies/Ready.One.mkv")
            self.assertEqual(runtime.workflow_summary(waiting["workflow_id"])["tasks"][1]["status"], "waiting_for_dependency")
            self.assertEqual(runtime.workflow_summary(ready_two["workflow_id"])["tasks"][0]["status"], "ready")

    def test_waiting_task_returns_to_fifo_position_after_resource_becomes_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            waiting = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:111",
                info_hash="waiting123",
                title="Waiting Movie",
                progress=0.1,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:00:00Z",
            )
            ready_one = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:222",
                video_path="/mnt/media/Movies/Ready.One.mkv",
                title="Ready One",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:01:00Z",
            )
            ready_two = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:333",
                video_path="/mnt/media/Movies/Ready.Two.mkv",
                title="Ready Two",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:02:00Z",
            )

            first_actions = runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T10:03:00Z")
            self.assertEqual(first_actions[0]["workflow_id"], ready_one["workflow_id"])
            ready_one_task_id = ready_one["tasks"][0]["task_id"]
            runtime.record_mst_job_created(
                workflow_id=ready_one["workflow_id"],
                task_id=ready_one_task_id,
                mst_job_id="job_ready_one",
                now="2026-06-11T10:03:01Z",
            )
            runtime.record_mst_job_status(
                workflow_id=ready_one["workflow_id"],
                task_id=ready_one_task_id,
                status="succeeded",
                result={"output": {"path": "/mnt/media/Movies/Ready.One.zh.ass"}},
                now="2026-06-11T10:04:00Z",
            )

            runtime.mark_qbitlarr_download_complete(
                info_hash="waiting123",
                content_path="/mnt/media/Movies/Waiting.Movie.mkv",
                now="2026-06-11T10:05:00Z",
            )
            second_actions = runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T10:05:01Z")

            self.assertEqual(len(second_actions), 1)
            self.assertEqual(second_actions[0]["workflow_id"], waiting["workflow_id"])
            self.assertEqual(runtime.workflow_summary(ready_two["workflow_id"])["tasks"][0]["status"], "ready")

    def test_active_subtitle_task_blocks_later_ready_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            active = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:111",
                video_path="/mnt/media/Movies/Active.mkv",
                title="Active Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:00:00Z",
            )
            active_actions = runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T10:01:00Z")
            self.assertEqual(len(active_actions), 1)
            later = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:222",
                video_path="/mnt/media/Movies/Later.mkv",
                title="Later Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:02:00Z",
            )

            blocked_actions = runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T10:03:00Z")

            self.assertEqual(blocked_actions, [])
            self.assertEqual(runtime.workflow_summary(active["workflow_id"])["tasks"][0]["status"], "dispatching")
            self.assertEqual(runtime.workflow_summary(later["workflow_id"])["tasks"][0]["status"], "ready")

    def test_queue_status_reports_global_counts_and_requester_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            active = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:other",
                video_path="/mnt/media/Movies/Active.mkv",
                title="Active Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:00:00Z",
            )
            active_actions = runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T10:00:30Z")
            active_task_id = active_actions[0]["task_id"]
            runtime.record_mst_job_created(
                workflow_id=active["workflow_id"],
                task_id=active_task_id,
                mst_job_id="job_active",
                now="2026-06-11T10:00:31Z",
            )
            runtime.record_mst_job_status(
                workflow_id=active["workflow_id"],
                task_id=active_task_id,
                status="running",
                status_detail={"stage": "translating"},
                now="2026-06-11T10:00:32Z",
            )
            waiting = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:me",
                info_hash="waiting123",
                title="Waiting Movie",
                progress=0.1,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:01:00Z",
            )
            runtime.record_local_video_subtitle_intent(
                requester_id="telegram:other",
                video_path="/mnt/media/Movies/Other.Ready.mkv",
                title="Other Ready",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:02:00Z",
            )
            own_ready = runtime.record_local_video_subtitle_intent(
                requester_id="telegram:me",
                video_path="/mnt/media/Movies/Own.Ready.mkv",
                title="Own Ready",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                now="2026-06-11T10:03:00Z",
            )

            status = runtime.queue_status(requester_id="telegram:me")

            self.assertEqual(status["global"]["active_count"], 1)
            self.assertEqual(status["global"]["ready_count"], 2)
            self.assertEqual(status["global"]["waiting_for_resource_count"], 1)
            self.assertEqual(status["global"]["total_open_count"], 4)
            by_title = {task["title"]: task for task in status["requester_tasks"]}
            self.assertEqual(by_title["Own Ready"]["workflow_id"], own_ready["workflow_id"])
            self.assertEqual(by_title["Own Ready"]["queue_position"], 2)
            self.assertEqual(by_title["Own Ready"]["tasks_ahead"], 2)
            self.assertEqual(by_title["Own Ready"]["resource_status"], "ready")
            self.assertEqual(by_title["Waiting Movie"]["workflow_id"], waiting["workflow_id"])
            self.assertEqual(by_title["Waiting Movie"]["queue_position"], None)
            self.assertEqual(by_title["Waiting Movie"]["resource_status"], "waiting")

    def test_mst_job_tracking_updates_task_and_workflow_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            workflow = runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.Movie.mkv",
            )
            attached = runtime.attach_subtitle_intent_to_current_download(
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            workflow_id = workflow["workflow_id"]
            task_id = attached["tasks"][1]["task_id"]

            runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T12:00:00Z")
            runtime.record_mst_job_created(
                workflow_id=workflow_id,
                task_id=task_id,
                mst_job_id="job_123",
                now="2026-06-11T12:00:02Z",
            )
            queued = runtime.workflow_summary(workflow_id)

            self.assertEqual(queued["status"], "running")
            self.assertEqual(queued["tasks"][1]["status"], "queued")
            self.assertEqual(queued["tasks"][1]["babelarr"]["job_id"], "job_123")

            runtime.record_mst_job_status(
                workflow_id=workflow_id,
                task_id=task_id,
                status="running",
                status_detail={"stage": "translating"},
                now="2026-06-11T12:00:03Z",
            )
            running = runtime.workflow_summary(workflow_id)
            self.assertEqual(running["status"], "running")
            self.assertEqual(running["tasks"][1]["status"], "running")
            self.assertEqual(running["tasks"][1]["babelarr"]["status_detail"], {"stage": "translating"})

            runtime.record_mst_job_status(
                workflow_id=workflow_id,
                task_id=task_id,
                status="succeeded",
                result={"output": {"path": "/mnt/media/Movies/Example.Movie.zh.ass"}},
                now="2026-06-11T12:00:04Z",
            )
            succeeded = runtime.workflow_summary(workflow_id)
            self.assertEqual(succeeded["status"], "succeeded")
            self.assertEqual(succeeded["tasks"][1]["status"], "succeeded")
            self.assertEqual(
                succeeded["tasks"][1]["babelarr"]["result"],
                {"output": {"path": "/mnt/media/Movies/Example.Movie.zh.ass"}},
            )

    def test_mst_job_result_promotes_resolved_video_file_over_download_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            workflow = runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                imdb_id="tt1234567",
                media_type="movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example Movie (2018) [1080p]",
            )
            attached = runtime.attach_subtitle_intent_to_current_download(
                requester_id="telegram:123",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            workflow_id = workflow["workflow_id"]
            task_id = attached["tasks"][1]["task_id"]

            runtime.claim_ready_mst_job_create_video_actions(now="2026-06-11T12:00:00Z")
            runtime.record_mst_job_created(
                workflow_id=workflow_id,
                task_id=task_id,
                mst_job_id="job_123",
                now="2026-06-11T12:00:02Z",
            )
            runtime.record_mst_job_status(
                workflow_id=workflow_id,
                task_id=task_id,
                status="succeeded",
                result={
                    "input": "/mnt/media/Movies/Example Movie (2018) [1080p]/Example.Movie.2018.1080p.BluRay.x264-GRP.mkv",
                    "output": "/mnt/media/Movies/Example Movie (2018) [1080p]/Example.Movie.2018.1080p.BluRay.x264-GRP.zh.ass",
                },
                now="2026-06-11T12:00:04Z",
            )

            succeeded = runtime.workflow_summary(workflow_id)
            self.assertEqual(
                succeeded["artifacts"]["video_path"],
                "/mnt/media/Movies/Example Movie (2018) [1080p]/Example.Movie.2018.1080p.BluRay.x264-GRP.mkv",
            )

    def test_ambiguous_followup_subtitle_request_requires_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MediaWorkflowRuntime(Path(tmp))
            runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="first",
                title="First Movie",
                imdb_id="tt1111111",
                media_type="movie",
                progress=0.2,
            )
            runtime.record_qbitlarr_download(
                requester_id="telegram:123",
                info_hash="second",
                title="Second Movie",
                imdb_id="tt2222222",
                media_type="movie",
                progress=0.3,
            )

            with self.assertRaisesRegex(RuntimeStoreError, "multiple active downloads"):
                runtime.attach_subtitle_intent_to_current_download(
                    requester_id="telegram:123",
                    source_language="en",
                    target_language="zh",
                    output_mode="bilingual-ass",
                )


if __name__ == "__main__":
    unittest.main()
