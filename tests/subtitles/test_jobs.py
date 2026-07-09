import tempfile
import unittest
import os
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from babelarr import cli as cli_module
from babelarr.cli import (
    build_parser,
    job_create_summary,
    job_create_video_summary,
    job_prune_summary,
    job_resume_summary,
    job_run_summary,
    job_show_summary,
    job_start_summary,
    translate_plex_args_from_job,
    translate_video_args_from_job,
)
from babelarr.jobs import (
    JobNeedsConfirmation,
    JobStore,
    JobStoreError,
    default_job_store_dir,
    run_job,
)
from babelarr.notifications import JobNotificationStore
from media_workflow_runtime import MediaWorkflowRuntime


def sample_request():
    return {
        "kind": "translate-plex",
        "plex": {
            "rating_key": "1468",
            "imdb": None,
            "season": None,
            "episode": None,
            "plex_base_url": "http://plex.test:32400",
        },
        "translation": {
            "source_language": "en",
            "target_language": "zh",
            "output_mode": "bilingual-ass",
            "backend": "fake",
            "model": "gpt-5.4-mini",
        },
        "output": {
            "output": None,
            "force": False,
            "work_dir": None,
            "keep_work_dir": False,
            "write_back": True,
            "refresh_plex": False,
        },
        "provider": {
            "online_subtitle_fallback": True,
            "subtitle_provider": "all",
            "provider_search_limit": 10,
            "download_provider_priority": "subdl,opensubtitles",
            "allow_low_confidence_subtitle": False,
        },
    }


class JobStoreTests(unittest.TestCase):
    def test_default_job_store_dir_is_persistent_user_data_dir(self):
        path = default_job_store_dir(env={}, home=Path("/home/example"))

        self.assertEqual(path, Path("/home/example/.local/share/mpilot/subtitles/jobs"))

    def test_env_can_override_default_job_store_dir(self):
        path = default_job_store_dir(env={"MST_JOB_STORE_DIR": "~/custom-jobs"}, home=Path("/home/example"))

        self.assertEqual(path, Path("/home/example/custom-jobs"))

    def test_babelarr_env_can_override_default_job_store_dir(self):
        path = default_job_store_dir(env={"BABELARR_JOB_STORE_DIR": "~/babelarr-jobs"}, home=Path("/home/example"))

        self.assertEqual(path, Path("/home/example/babelarr-jobs"))

    def test_mpilot_env_can_override_default_job_store_dir(self):
        path = default_job_store_dir(env={"MPILOT_SUBTITLE_JOB_STORE_DIR": "~/mpilot-jobs"}, home=Path("/home/example"))

        self.assertEqual(path, Path("/home/example/mpilot-jobs"))

    def test_parser_prefers_mpilot_subtitle_language_env_names(self):
        with patch.dict(
            os.environ,
            {
                "MPILOT_SUBTITLE_SOURCE_LANGUAGE": "fr",
                "MPILOT_SUBTITLE_TARGET_LANGUAGE": "zh",
                "MPILOT_SOURCE_LANGUAGE": "remote-source-namespace",
                "BABELARR_SOURCE_LANGUAGE": "en",
                "BABELARR_TARGET_LANGUAGE": "ja",
            },
            clear=False,
        ):
            parser = build_parser()
            args = parser.parse_args(
                [
                    "translate-plex",
                    "--rating-key",
                    "1468",
                    "--plex-base-url",
                    "http://plex.test:32400",
                    "--plex-token",
                    "token",
                ]
            )

        self.assertEqual(args.source_language, "fr")
        self.assertEqual(args.target_language, "zh")

    def test_create_persists_translate_plex_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))

            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            loaded = store.get(job["job_id"])

            self.assertEqual(job["schema_version"], 1)
            self.assertEqual(job["status"], "queued")
            self.assertEqual(job["job_type"], "translate-plex")
            self.assertEqual(job["created_at"], "2026-06-10T12:00:00Z")
            self.assertEqual(loaded["request"]["plex"]["rating_key"], "1468")
            self.assertTrue((Path(tmp) / ("%s.json" % job["job_id"])).exists())

    def test_save_uses_unique_temp_file_without_clobbering_existing_tmp(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            stale_tmp = Path(tmp) / ("%s.json.tmp" % job["job_id"])
            stale_tmp.write_text("another writer temp file\n", encoding="utf-8")

            job["status"] = "running"
            store.save(job)

            self.assertEqual(stale_tmp.read_text(encoding="utf-8"), "another writer temp file\n")
            self.assertEqual(store.get(job["job_id"])["status"], "running")

    def test_prune_deletes_old_orphan_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            old_tmp = Path(tmp) / ".job_orphan.json.deadbeef.tmp"
            recent_tmp = Path(tmp) / ".job_recent.json.deadbeef.tmp"
            old_tmp.write_text("orphan\n", encoding="utf-8")
            recent_tmp.write_text("recent\n", encoding="utf-8")
            old_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp()
            recent_time = datetime(2026, 6, 1, 12, tzinfo=timezone.utc).timestamp()
            os.utime(old_tmp, (old_time, old_time))
            os.utime(recent_tmp, (recent_time, recent_time))

            summary = store.prune(
                now=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
                retention=timedelta(days=90),
            )

            self.assertEqual(summary["count"], 0)
            self.assertFalse(old_tmp.exists())
            self.assertTrue(recent_tmp.exists())

    def test_run_job_records_success_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")

            result = run_job(
                store,
                job["job_id"],
                lambda current: {"output": "/media/Movie.zh.ass", "job_id": current["job_id"]},
                now="2026-06-10T12:01:00Z",
            )

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["result"]["output"], "/media/Movie.zh.ass")
            self.assertEqual(result["attempts"][0]["status"], "succeeded")
            self.assertEqual(result["attempts"][0]["started_at"], "2026-06-10T12:01:00Z")

    def test_mark_progress_updates_running_job_status_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            store.mark_running(job["job_id"], now="2026-06-10T12:01:00Z")

            updated = store.mark_progress(
                job["job_id"],
                {
                    "stage": "translating",
                    "message": "Translating subtitles.",
                    "details": {"chunk_index": 3, "total_chunks": 14},
                },
                now="2026-06-10T12:02:00Z",
            )

            self.assertEqual(updated["progress"]["stage"], "translating")
            self.assertEqual(updated["progress"]["details"]["chunk_index"], 3)
            self.assertEqual(updated["progress"]["updated_at"], "2026-06-10T12:02:00Z")
            self.assertEqual(updated["attempts"][0]["progress"]["details"]["total_chunks"], 14)
            self.assertEqual(updated["updated_at"], "2026-06-10T12:02:00Z")

    def test_job_show_summary_includes_running_progress_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            store.mark_running(job["job_id"], now="2026-06-10T12:01:00Z")
            store.mark_progress(
                job["job_id"],
                {
                    "stage": "translating",
                    "message": "Translating subtitle chunk 3 of 14.",
                    "details": {"chunk_index": 3, "completed_chunks": 2, "total_chunks": 14, "cue_count": 1400},
                },
                now="2026-06-10T12:02:00Z",
            )
            parser = build_parser()
            args = parser.parse_args(["job-show", "--job-store-dir", tmp, job["job_id"]])

            summary = job_show_summary(args)

            self.assertEqual(summary["status_detail"]["stage"], "translating")
            self.assertEqual(summary["status_detail"]["source_language"], "en")
            self.assertEqual(summary["status_detail"]["target_language"], "zh")
            self.assertEqual(summary["status_detail"]["translation_progress"]["chunk_index"], 3)
            self.assertEqual(summary["status_detail"]["translation_progress"]["completed_chunks"], 2)
            self.assertEqual(summary["status_detail"]["translation_progress"]["total_chunks"], 14)

    def test_job_show_summary_derives_legacy_succeeded_provider_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            store.mark_running(job["job_id"], now="2026-06-10T12:01:00Z")
            succeeded = store.mark_succeeded(
                job["job_id"],
                {
                    "source_language": "en",
                    "target_language": "zh",
                    "output_mode": "single-srt",
                    "output": ".runtime/Movie.zh.srt",
                    "write_back": {
                        "requested": True,
                        "status": "written",
                        "path": "/media/Movie.zh.srt",
                    },
                    "translation": {
                        "source_acquisition": {
                            "status": "ready",
                            "method": "provider:subdl",
                            "provider_fallback": {
                                "candidate": {
                                    "provider": "subdl",
                                    "language": "en",
                                    "file_name": "Movie.WEBRip.en.srt",
                                },
                                "match": {"confidence": "high", "score": 95},
                            },
                        },
                        "translation_summary": {
                            "cue_count": 1712,
                            "chunks": 18,
                            "chunk_summaries": [{"index": index} for index in range(1, 19)],
                        },
                    },
                },
                now="2026-06-10T12:02:00Z",
            )
            succeeded.pop("progress", None)
            succeeded.pop("progress_events", None)
            succeeded.pop("progress_milestones", None)
            store.save(succeeded)
            parser = build_parser()
            args = parser.parse_args(["job-show", "--job-store-dir", tmp, job["job_id"]])

            summary = job_show_summary(args)

            self.assertEqual(summary["status_detail"]["stage"], "succeeded")
            self.assertEqual(summary["status_detail"]["source_subtitle"]["method"], "provider:subdl")
            self.assertEqual(summary["status_detail"]["source_subtitle"]["provider"], "subdl")
            self.assertEqual(summary["status_detail"]["source_subtitle"]["language"], "en")
            self.assertEqual(summary["status_detail"]["translation_progress"]["completed_chunks"], 18)
            self.assertEqual(summary["status_detail"]["translation_progress"]["total_chunks"], 18)
            self.assertEqual(summary["status_detail"]["output"]["write_back_status"], "written")

    def test_run_job_rejects_succeeded_jobs_without_new_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            store.mark_running(job["job_id"], now="2026-06-10T12:01:00Z")
            store.mark_succeeded(job["job_id"], {"output": "/media/Movie.zh.ass"}, now="2026-06-10T12:02:00Z")

            with self.assertRaisesRegex(JobStoreError, "already succeeded"):
                run_job(
                    store,
                    job["job_id"],
                    lambda _job: {"output": "/media/Movie.zh.ass"},
                    now="2026-06-10T12:03:00Z",
                )

            loaded = store.get(job["job_id"])
            self.assertEqual(loaded["status"], "succeeded")
            self.assertEqual(len(loaded["attempts"]), 1)

    def test_run_job_rejects_running_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            store.mark_running(job["job_id"], now="2026-06-10T12:01:00Z")
            calls = []

            with self.assertRaisesRegex(JobStoreError, "already running"):
                run_job(
                    store,
                    job["job_id"],
                    lambda _job: calls.append("ran") or {"output": "/media/Movie.zh.ass"},
                    now="2026-06-10T12:02:00Z",
                )

            self.assertEqual(calls, [])
            self.assertEqual(store.get(job["job_id"])["status"], "running")

    def test_run_job_rejects_when_job_lock_is_already_held(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")

            with store.lock(job["job_id"]):
                with self.assertRaisesRegex(JobStoreError, "already locked"):
                    run_job(
                        store,
                        job["job_id"],
                        lambda _job: {"output": "/media/Movie.zh.ass"},
                        now="2026-06-10T12:01:00Z",
                    )

            loaded = store.get(job["job_id"])
            self.assertEqual(loaded["status"], "queued")
            self.assertEqual(loaded["attempts"], [])

    def test_run_job_records_failure_without_losing_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())

            result = run_job(
                store,
                job["job_id"],
                lambda _job: (_ for _ in ()).throw(RuntimeError("boom")),
                now="2026-06-10T12:01:00Z",
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["last_error"]["message"], "boom")
            self.assertEqual(result["request"]["plex"]["rating_key"], "1468")

    def test_run_job_records_needs_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())

            result = run_job(
                store,
                job["job_id"],
                lambda _job: (_ for _ in ()).throw(
                    JobNeedsConfirmation({"action": "confirm_low_confidence_subtitle"})
                ),
                now="2026-06-10T12:01:00Z",
            )

            self.assertEqual(result["status"], "needs_confirmation")
            self.assertEqual(result["needs_confirmation"]["action"], "confirm_low_confidence_subtitle")
            self.assertEqual(result["attempts"][0]["status"], "needs_confirmation")

    def test_run_job_requires_explicit_confirmation_for_needs_confirmation_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())
            store.mark_running(job["job_id"], now="2026-06-10T12:01:00Z")
            store.mark_needs_confirmation(
                job["job_id"],
                {"action": "confirm_low_confidence_subtitle"},
                now="2026-06-10T12:02:00Z",
            )

            with self.assertRaisesRegex(JobStoreError, "needs confirmation"):
                run_job(
                    store,
                    job["job_id"],
                    lambda _job: {"output": "/media/Movie.zh.ass"},
                    now="2026-06-10T12:03:00Z",
                )

            result = run_job(
                store,
                job["job_id"],
                lambda _job: {"output": "/media/Movie.zh.ass"},
                now="2026-06-10T12:04:00Z",
                allow_needs_confirmation=True,
            )

            self.assertEqual(result["status"], "succeeded")

    def test_recoverable_jobs_include_queued_failed_and_stale_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            queued = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            failed = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            running = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            succeeded = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            confirm = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            store.mark_failed(failed["job_id"], RuntimeError("fail"), now="2026-06-10T12:01:00Z")
            store.mark_running(running["job_id"], now="2026-06-10T11:00:00Z")
            store.mark_succeeded(succeeded["job_id"], {"ok": True}, now="2026-06-10T12:01:00Z")
            store.mark_needs_confirmation(
                confirm["job_id"],
                {"action": "confirm_low_confidence_subtitle"},
                now="2026-06-10T12:01:00Z",
            )

            recoverable = store.recoverable_jobs(
                now=datetime(2026, 6, 10, 12, 30, tzinfo=timezone.utc),
                stale_after=timedelta(minutes=30),
            )

            self.assertEqual(
                {job["job_id"] for job in recoverable},
                {queued["job_id"], failed["job_id"], running["job_id"]},
            )

    def test_prune_deletes_only_old_succeeded_jobs_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            old_success = store.create("translate-plex", sample_request(), now="2026-01-01T12:00:00Z")
            recent_success = store.create("translate-plex", sample_request(), now="2026-05-15T12:00:00Z")
            old_failed = store.create("translate-plex", sample_request(), now="2026-01-01T12:00:00Z")
            old_confirm = store.create("translate-plex", sample_request(), now="2026-01-01T12:00:00Z")
            store.mark_succeeded(old_success["job_id"], {"ok": True}, now="2026-01-01T12:01:00Z")
            store.mark_succeeded(recent_success["job_id"], {"ok": True}, now="2026-05-15T12:01:00Z")
            store.mark_failed(old_failed["job_id"], RuntimeError("fail"), now="2026-01-01T12:01:00Z")
            store.mark_needs_confirmation(
                old_confirm["job_id"],
                {"action": "confirm_low_confidence_subtitle"},
                now="2026-01-01T12:01:00Z",
            )

            summary = store.prune(
                now=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
                retention=timedelta(days=90),
            )

            self.assertEqual(summary["count"], 1)
            self.assertEqual(summary["jobs"][0]["job_id"], old_success["job_id"])
            with self.assertRaises(JobStoreError):
                store.get(old_success["job_id"])
            self.assertEqual(store.get(recent_success["job_id"])["status"], "succeeded")
            self.assertEqual(store.get(old_failed["job_id"])["status"], "failed")
            self.assertEqual(store.get(old_confirm["job_id"])["status"], "needs_confirmation")

    def test_prune_deletes_lock_file_for_pruned_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            old_success = store.create("translate-plex", sample_request(), now="2026-01-01T12:00:00Z")
            store.mark_succeeded(old_success["job_id"], {"ok": True}, now="2026-01-01T12:01:00Z")
            lock_path = Path(tmp) / ("%s.json.lock" % old_success["job_id"])
            lock_path.write_text("", encoding="utf-8")

            store.prune(
                now=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
                retention=timedelta(days=90),
            )

            self.assertFalse(lock_path.exists())

    def test_prune_dry_run_keeps_candidate_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            old_success = store.create("translate-plex", sample_request(), now="2026-01-01T12:00:00Z")
            store.mark_succeeded(old_success["job_id"], {"ok": True}, now="2026-01-01T12:01:00Z")

            summary = store.prune(
                now=datetime(2026, 6, 10, 12, tzinfo=timezone.utc),
                retention=timedelta(days=90),
                dry_run=True,
            )

            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["count"], 1)
            self.assertEqual(store.get(old_success["job_id"])["status"], "succeeded")

    def test_cli_parser_accepts_job_commands(self):
        parser = build_parser()

        created = parser.parse_args(
            [
                "job-create",
                "--rating-key",
                "1468",
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--write-back",
            ]
        )
        shown = parser.parse_args(["job-show", "job_123"])
        listed = parser.parse_args(["job-list", "--status", "failed"])
        run = parser.parse_args(["job-run", "job_123", "--plex-token", "token"])
        start = parser.parse_args(["job-start", "job_123", "--plex-token", "token"])
        resume = parser.parse_args(["job-resume", "--stale-after-seconds", "60"])
        prune = parser.parse_args(["job-prune", "--dry-run"])

        self.assertEqual(created.command, "job-create")
        self.assertEqual(created.rating_key, "1468")
        self.assertTrue(created.write_back)
        self.assertEqual(shown.job_id, "job_123")
        self.assertEqual(listed.status, "failed")
        self.assertEqual(run.plex_token, "token")
        self.assertEqual(start.command, "job-start")
        self.assertEqual(start.plex_token, "token")
        self.assertEqual(resume.stale_after_seconds, 60)
        self.assertEqual(prune.command, "job-prune")
        self.assertEqual(prune.retention_days, 90)
        self.assertTrue(prune.dry_run)

    def test_job_create_summary_writes_job_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = build_parser()
            args = parser.parse_args(
                [
                    "job-create",
                    "--job-store-dir",
                    tmp,
                    "--rating-key",
                    "1468",
                    "--target-language",
                    "zh",
                    "--assume-unlabeled-stream-language",
                    "--write-back",
                ]
            )

            summary = job_create_summary(args)
            loaded = JobStore(Path(tmp)).get(summary["job"]["job_id"])

            self.assertEqual(summary["job"]["status"], "queued")
            self.assertEqual(loaded["request"]["plex"]["rating_key"], "1468")
            self.assertTrue(loaded["request"]["translation"]["assume_unlabeled_stream_language"])
            self.assertTrue(loaded["request"]["output"]["write_back"])
            run_args = translate_plex_args_from_job(loaded, Namespace())
            self.assertTrue(run_args.assume_unlabeled_stream_language)

    def test_job_run_summary_uses_executor_and_updates_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())
            parser = build_parser()
            args = parser.parse_args(["job-run", "--job-store-dir", tmp, job["job_id"]])

            def executor(current, run_args):
                run_args.progress_callback({"stage": "resolving_media", "message": "Resolving Plex item."})
                return {"output": "/media/Movie.zh.ass", "job_id": current["job_id"]}

            summary = job_run_summary(
                args,
                executor=executor,
            )

            self.assertEqual(summary["job"]["status"], "succeeded")
            loaded = JobStore(Path(tmp)).get(job["job_id"])
            self.assertEqual(loaded["result"]["output"], "/media/Movie.zh.ass")
            self.assertEqual(loaded["progress"]["stage"], "succeeded")
            self.assertEqual(loaded["progress_milestones"]["resolving_media"]["stage"], "resolving_media")
            self.assertEqual(loaded["progress_events"][-1]["stage"], "succeeded")

    def test_job_run_summary_mirrors_terminal_status_to_runtime_when_context_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = MediaWorkflowRuntime(root / "runtime")
            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.mkv",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            workflow_id = workflow["workflow_id"]
            task_id = workflow["tasks"][1]["task_id"]
            store = JobStore(root / "jobs")
            job = store.create("translate-plex", sample_request())
            runtime.record_mst_job_created(workflow_id=workflow_id, task_id=task_id, mst_job_id=job["job_id"])
            parser = build_parser()
            args = parser.parse_args(["job-run", "--job-store-dir", str(root / "jobs"), job["job_id"]])

            def executor(current, _run_args):
                return {"output": "/mnt/media/Movies/Example.zh.ass", "job_id": current["job_id"]}

            with patch.dict(
                os.environ,
                {
                    "MST_RUNTIME_STORE_DIR": str(root / "runtime"),
                    "MST_RUNTIME_WORKFLOW_ID": workflow_id,
                    "MST_RUNTIME_TASK_ID": task_id,
                },
                clear=False,
            ):
                job_run_summary(args, executor=executor)

            mirrored = runtime.workflow_summary(workflow_id)
            subtitle_task = mirrored["tasks"][1]
            self.assertEqual(mirrored["status"], "succeeded")
            self.assertEqual(subtitle_task["status"], "succeeded")
            self.assertEqual(subtitle_task["babelarr"]["status"], "succeeded")
            self.assertEqual(subtitle_task["babelarr"]["result"]["output"], "/mnt/media/Movies/Example.zh.ass")

    def test_job_run_summary_advances_runtime_queue_after_terminal_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = MediaWorkflowRuntime(root / "runtime")
            workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
                requester_id="telegram:123",
                info_hash="abc123",
                title="Example Movie",
                progress=1.0,
                content_path="/mnt/media/Movies/Example.mkv",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            runtime.record_local_video_subtitle_intent(
                requester_id="telegram:456",
                video_path="/mnt/media/Movies/Next.mkv",
                title="Next Movie",
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
            )
            workflow_id = workflow["workflow_id"]
            task_id = workflow["tasks"][1]["task_id"]
            store = JobStore(root / "jobs")
            job = store.create("translate-plex", sample_request())
            runtime.record_mst_job_created(workflow_id=workflow_id, task_id=task_id, mst_job_id=job["job_id"])
            parser = build_parser()
            args = parser.parse_args(["job-run", "--job-store-dir", str(root / "jobs"), job["job_id"]])

            def executor(current, _run_args):
                return {"output": "/mnt/media/Movies/Example.zh.ass", "job_id": current["job_id"]}

            with patch.dict(
                os.environ,
                {
                    "MST_RUNTIME_STORE_DIR": str(root / "runtime"),
                    "MST_RUNTIME_WORKFLOW_ID": workflow_id,
                    "MST_RUNTIME_TASK_ID": task_id,
                },
                clear=False,
            ), patch("media_workflow_runtime.dispatcher.dispatch_ready_mst_actions", return_value={"status": "ok"}) as dispatch:
                job_run_summary(args, executor=executor)

            dispatch.assert_called_once()
            self.assertEqual(str(dispatch.call_args.args[0].root), str(root / "runtime"))
            self.assertEqual(dispatch.call_args.kwargs["job_store_dir"], str(root / "jobs"))
            self.assertEqual(dispatch.call_args.kwargs["openai_base_url"], args.openai_base_url)
            self.assertEqual(dispatch.call_args.kwargs["openai_api_key"], args.openai_api_key)

    def test_job_start_summary_spawns_background_worker_without_secret_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())
            parser = build_parser()
            args = parser.parse_args(
                [
                    "job-start",
                    "--job-store-dir",
                    tmp,
                    "--plex-token",
                    "plex-secret",
                    "--openai-api-key",
                    "openai-secret",
                    "--opensubtitles-password",
                    "os-password",
                    "--subdl-api-key",
                    "subdl-secret",
                    job["job_id"],
                ]
            )
            captured = {}

            class FakeProcess:
                pid = 12345

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                return FakeProcess()

            summary = job_start_summary(args, popen=fake_popen)

            self.assertEqual(summary["status"], "started")
            self.assertEqual(summary["worker"]["pid"], 12345)
            self.assertEqual(summary["job"]["status"], "queued")
            command_text = " ".join(captured["command"])
            self.assertIn("job-run", captured["command"])
            self.assertIn(job["job_id"], captured["command"])
            self.assertNotIn("plex-secret", command_text)
            self.assertNotIn("openai-secret", command_text)
            self.assertNotIn("os-password", command_text)
            self.assertNotIn("subdl-secret", command_text)
            self.assertEqual(captured["kwargs"]["env"]["PLEX_TOKEN"], "plex-secret")
            self.assertEqual(captured["kwargs"]["env"]["OPENAI_API_KEY"], "openai-secret")
            self.assertEqual(captured["kwargs"]["env"]["OPENSUBTITLES_PASSWORD"], "os-password")
            self.assertEqual(captured["kwargs"]["env"]["SUBDL_API_KEY"], "subdl-secret")
            self.assertTrue(summary["worker"]["stdout_log"].endswith(".out.log"))
            self.assertTrue(summary["worker"]["stderr_log"].endswith(".err.log"))

    def test_job_start_summary_registers_notification_watch_without_worker_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())
            parser = build_parser()
            args = parser.parse_args(
                [
                    "job-start",
                    "--job-store-dir",
                    tmp,
                    "--notification-target",
                    "telegram:123",
                    "--requester-id",
                    "telegram:123",
                    "--notification-title",
                    "Example Movie",
                    "--notification-language",
                    "zh",
                    job["job_id"],
                ]
            )
            captured = {}

            class FakeProcess:
                pid = 12345

            def fake_popen(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                return FakeProcess()

            notification_calls = []

            class FakeNotifier:
                def register_watch(self, **kwargs):
                    notification_calls.append(("register", kwargs))
                    return {"job_id": kwargs["job_id"], "notification_target": kwargs["notification_target"]}

            with patch.dict(
                os.environ,
                {"MPILOT_SUBTITLE_JOB_NOTIFICATION_INITIAL_DELAY_SECONDS": "45"},
                clear=False,
            ), patch(
                "babelarr.notifications.JobCompletionNotifier.from_env",
                return_value=FakeNotifier(),
            ), patch(
                "babelarr.notifications.start_notification_daemon_from_env",
                side_effect=lambda: notification_calls.append(("start_daemon", None)),
            ), patch(
                "babelarr.notifications.touch_notification_wake_file",
                side_effect=lambda: notification_calls.append(("wake", None)),
            ):
                summary = job_start_summary(args, popen=fake_popen)

            self.assertEqual(summary["status"], "started")
            env = captured["kwargs"]["env"]
            self.assertNotIn("MST_JOB_NOTIFICATION_TARGET", env)
            self.assertNotIn("MST_JOB_NOTIFICATION_REQUESTER_ID", env)
            self.assertNotIn("MST_JOB_NOTIFICATION_TITLE", env)
            self.assertNotIn("MST_JOB_NOTIFICATION_LANGUAGE", env)
            self.assertEqual(summary["notification_watch"]["status"], "watching")
            self.assertEqual(notification_calls[0][0], "register")
            self.assertEqual(notification_calls[0][1]["job_id"], job["job_id"])
            self.assertEqual(notification_calls[0][1]["job_store_dir"], str(Path(tmp)))
            self.assertEqual(notification_calls[0][1]["notification_target"], "telegram:123")
            self.assertEqual(notification_calls[0][1]["requester_id"], "telegram:123")
            self.assertEqual(notification_calls[0][1]["title"], "Example Movie")
            self.assertEqual(notification_calls[0][1]["language"], "zh")
            self.assertEqual(notification_calls[0][1]["initial_notification_delay_seconds"], 45.0)
            self.assertIn(("wake", None), notification_calls)
            self.assertIn(("start_daemon", None), notification_calls)

    def test_job_run_summary_touches_notification_wake_file_after_worker_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())
            parser = build_parser()
            args = parser.parse_args(["job-run", "--job-store-dir", tmp, job["job_id"]])
            calls = []

            env = {
                "MST_JOB_NOTIFICATION_TARGET": "telegram:123",
                "MST_JOB_NOTIFICATION_REQUESTER_ID": "telegram:123",
                "MST_JOB_NOTIFICATION_TITLE": "Example Movie",
                "MST_JOB_NOTIFICATION_LANGUAGE": "zh",
                "MST_JOB_NOTIFICATION_WATCHES_PATH": str(Path(tmp) / "watches.json"),
            }
            with patch.dict(os.environ, env, clear=False), patch(
                "babelarr.notifications.JobCompletionNotifier.from_env",
                side_effect=AssertionError("worker must not start its own notifier"),
            ), patch(
                "babelarr.notifications.touch_notification_wake_file",
                side_effect=lambda: calls.append(("wake", None)),
            ), patch(
                "babelarr.notifications.start_notification_daemon_from_env",
                side_effect=lambda: calls.append(("start_daemon", None)),
            ):
                summary = job_run_summary(args, executor=lambda _current, _args: {"output": "/media/Movie.zh.ass"})

            self.assertEqual(summary["job"]["status"], "succeeded")
            # No pending watches in the store, so the worker must not spawn a daemon.
            self.assertEqual(calls, [("wake", None)])

    def test_job_run_summary_spawns_notification_daemon_when_watches_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "jobs")
            job = store.create("translate-plex", sample_request())
            watches_path = Path(tmp) / "watches.json"
            JobNotificationStore(watches_path).upsert_watch(
                job_id=job["job_id"],
                job_store_dir=Path(tmp) / "jobs",
                notification_target="telegram:123",
            )
            parser = build_parser()
            args = parser.parse_args(["job-run", "--job-store-dir", str(Path(tmp) / "jobs"), job["job_id"]])
            calls = []

            env = {"MST_JOB_NOTIFICATION_WATCHES_PATH": str(watches_path)}
            with patch.dict(os.environ, env, clear=False), patch(
                "babelarr.notifications.touch_notification_wake_file",
                side_effect=lambda: calls.append(("wake", None)),
            ), patch(
                "babelarr.notifications.start_notification_daemon_from_env",
                side_effect=lambda: calls.append(("start_daemon", None)),
            ):
                summary = job_run_summary(args, executor=lambda _current, _args: {"output": "/media/Movie.zh.ass"})

            self.assertEqual(summary["job"]["status"], "succeeded")
            self.assertEqual(calls, [("wake", None), ("start_daemon", None)])

    def test_job_start_summary_reports_already_running_without_spawning(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())
            store.mark_running(job["job_id"])
            parser = build_parser()
            args = parser.parse_args(["job-start", "--job-store-dir", tmp, job["job_id"]])

            summary = job_start_summary(
                args,
                popen=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not spawn")),
            )

            self.assertEqual(summary["status"], "already_running")
            self.assertEqual(summary["job"]["status"], "running")

    def test_notify_daemon_command_runs_notification_daemon(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "notify-daemon",
                "--once",
                "--idle-exit-seconds",
                "1",
                "--poll-interval-seconds",
                "0.2",
            ]
        )
        with patch(
            "babelarr.notifications.run_notification_daemon",
            return_value={"status": "ran_once"},
        ) as run:
            summary = cli_module.summary_from_args(args)

        self.assertEqual(summary, {"status": "ran_once"})
        self.assertTrue(run.call_args.kwargs["run_once"])
        self.assertEqual(run.call_args.kwargs["idle_exit_seconds"], 1)
        self.assertEqual(run.call_args.kwargs["poll_interval_seconds"], 0.2)
        self.assertEqual(run.call_args.kwargs["lock_acquire_timeout_seconds"], 3.0)

    def test_job_run_summary_persists_low_confidence_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())
            store.mark_running(job["job_id"], now="2026-06-10T12:01:00Z")
            store.mark_needs_confirmation(
                job["job_id"],
                {"action": "confirm_low_confidence_subtitle"},
                now="2026-06-10T12:02:00Z",
            )
            parser = build_parser()
            args = parser.parse_args(
                [
                    "job-run",
                    "--job-store-dir",
                    tmp,
                    "--allow-low-confidence-subtitle",
                    job["job_id"],
                ]
            )
            seen_confirmation = []

            summary = job_run_summary(
                args,
                executor=lambda current, _args: seen_confirmation.append(
                    current["request"]["provider"]["allow_low_confidence_subtitle"]
                )
                or {"output": "/media/Movie.zh.ass"},
            )

            loaded = JobStore(Path(tmp)).get(job["job_id"])
            self.assertEqual(summary["job"]["status"], "succeeded")
            self.assertEqual(seen_confirmation, [True])
            self.assertTrue(loaded["request"]["provider"]["allow_low_confidence_subtitle"])
            self.assertIsNotNone(loaded["low_confidence_confirmed_at"])

    def test_job_run_summary_does_not_persist_confirmation_for_succeeded_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request())
            store.mark_running(job["job_id"], now="2026-06-10T12:01:00Z")
            store.mark_succeeded(job["job_id"], {"output": "/media/Movie.zh.ass"}, now="2026-06-10T12:02:00Z")
            parser = build_parser()
            args = parser.parse_args(
                [
                    "job-run",
                    "--job-store-dir",
                    tmp,
                    "--allow-low-confidence-subtitle",
                    job["job_id"],
                ]
            )

            with self.assertRaisesRegex(JobStoreError, "already succeeded"):
                job_run_summary(args, executor=lambda _current, _args: {"output": "/media/Movie.zh.ass"})

            loaded = JobStore(Path(tmp)).get(job["job_id"])
            self.assertFalse(loaded["request"]["provider"]["allow_low_confidence_subtitle"])
            self.assertNotIn("low_confidence_confirmed_at", loaded)

    def test_job_resume_summary_skips_conflicted_job_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            first = store.create("translate-plex", sample_request(), now="2026-06-10T12:00:00Z")
            second = store.create("translate-plex", sample_request(), now="2026-06-10T12:01:00Z")
            parser = build_parser()
            args = parser.parse_args(["job-resume", "--job-store-dir", tmp, "--limit", "10"])
            calls = []

            def fake_job_run(run_args, executor=None, allow_running=False):
                calls.append(run_args.job_id)
                if run_args.job_id == first["job_id"]:
                    raise JobStoreError("job is already locked by another worker: %s" % run_args.job_id)
                return {"job": {"job_id": run_args.job_id, "status": "succeeded"}}

            with patch.object(cli_module, "job_run_summary", side_effect=fake_job_run):
                summary = job_resume_summary(args)

            self.assertEqual(calls, [first["job_id"], second["job_id"]])
            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["jobs"][0]["status"], "skipped")
            self.assertIn("already locked", summary["jobs"][0]["reason"])
            self.assertEqual(summary["jobs"][1], {"job_id": second["job_id"], "status": "succeeded"})

    def test_job_prune_summary_deletes_old_success_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create("translate-plex", sample_request(), now="2026-01-01T12:00:00Z")
            store.mark_succeeded(job["job_id"], {"ok": True}, now="2026-01-01T12:01:00Z")
            parser = build_parser()
            args = parser.parse_args(["job-prune", "--job-store-dir", tmp])

            summary = job_prune_summary(args, now=datetime(2026, 6, 10, 12, tzinfo=timezone.utc))

            self.assertEqual(summary["retention_days"], 90)
            self.assertEqual(summary["count"], 1)
            self.assertEqual(summary["jobs"][0]["job_id"], job["job_id"])
            with self.assertRaises(JobStoreError):
                JobStore(Path(tmp)).get(job["job_id"])


    def test_job_create_video_writes_job_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            parser = build_parser()
            args = parser.parse_args(
                [
                    "job-create-video",
                    "--job-store-dir",
                    tmp,
                    "--video-path",
                    "/media/Movie.mkv",
                    "--imdb-id",
                    "tt1234567",
                    "--title",
                    "My Movie",
                    "--media-type",
                    "episode",
                    "--season",
                    "9",
                    "--episode",
                    "4",
                    "--target-language",
                    "zh",
                ]
            )

            summary = job_create_video_summary(args)
            loaded = JobStore(Path(tmp)).get(summary["job"]["job_id"])

            self.assertEqual(summary["job"]["status"], "queued")
            self.assertEqual(loaded["request"]["kind"], "translate-video")
            self.assertEqual(loaded["request"]["video"]["video_path"], "/media/Movie.mkv")
            self.assertEqual(loaded["request"]["video"]["imdb_id"], "tt1234567")
            self.assertEqual(loaded["request"]["video"]["title"], "My Movie")
            self.assertEqual(loaded["request"]["video"]["media_type"], "episode")
            self.assertEqual(loaded["request"]["video"]["season"], 9)
            self.assertEqual(loaded["request"]["video"]["episode"], 4)
            self.assertEqual(loaded["request"]["translation"]["target_language"], "zh")

    def test_translate_video_args_from_job_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create(
                "translate-video",
                {
                    "kind": "translate-video",
                    "video": {
                        "video_path": "/media/Movie.mkv",
                        "imdb_id": "tt1234567",
                        "title": "My Movie",
                        "media_type": "movie",
                    },
                    "translation": {
                        "source_language": "en",
                        "target_language": "zh",
                        "output_mode": "bilingual-ass",
                        "backend": "codex-cli",
                        "model": "gpt-5.4-mini",
                        "assume_unlabeled_stream_language": True,
                    },
                    "render": {"primary_script": "cjk", "secondary_script": "latin", "ass_height": 1080},
                    "output": {"output": None, "force": False, "work_dir": None, "keep_work_dir": False},
                    "provider": {
                        "online_subtitle_fallback": True,
                        "subtitle_provider": "all",
                        "provider_search_limit": 10,
                        "download_provider_priority": "opensubtitles,subdl",
                        "allow_low_confidence_subtitle": False,
                        "allow_provider_fallback_language": False,
                    },
                },
            )

            args = translate_video_args_from_job(job, Namespace())

            self.assertEqual(args.video_path, "/media/Movie.mkv")
            self.assertEqual(args.imdb_id, "tt1234567")
            self.assertEqual(args.title, "My Movie")
            self.assertEqual(args.season, None)
            self.assertEqual(args.episode, None)
            self.assertEqual(args.source_language, "en")
            self.assertEqual(args.target_language, "zh")
            self.assertTrue(args.assume_unlabeled_stream_language)
            self.assertFalse(args.no_online_subtitle_fallback)

    def test_translate_video_job_executor_infers_episode_numbers_from_release_name(self):
        job = {
            "request": {
                "kind": "translate-video",
                "video": {
                    "video_path": "/media/Rick.and.Morty.S09E04.1080p.HEVC.x265-MeGusta[EZTVx.to].mkv",
                    "imdb_id": "tt43095689",
                    "title": "Rick and Morty S09E04 - A Ricker Runs Through It",
                    "media_type": "episode",
                },
                "translation": {
                    "source_language": "en",
                    "target_language": "zh",
                    "output_mode": "bilingual-ass",
                    "backend": "codex-cli",
                    "model": "gpt-5.4-mini",
                    "assume_unlabeled_stream_language": False,
                },
                "render": {"primary_script": "cjk", "secondary_script": "latin", "ass_height": 1080},
                "output": {"output": None, "force": False, "work_dir": None, "keep_work_dir": False},
                "provider": {
                    "online_subtitle_fallback": True,
                    "subtitle_provider": "all",
                    "provider_search_limit": 10,
                    "download_provider_priority": "subdl,opensubtitles",
                    "allow_low_confidence_subtitle": False,
                    "allow_provider_fallback_language": False,
                },
            }
        }
        captured = {}

        def fake_translate_video_file(video_path, options, resolved_media=None):
            captured["video_path"] = video_path
            captured["resolved_media"] = resolved_media
            return {"output": "/media/Rick.and.Morty.S09E04.zh.ass"}

        with patch.object(cli_module, "translate_video_file", side_effect=fake_translate_video_file):
            result = cli_module.translate_video_job_executor(job, Namespace())

        self.assertEqual(result["output"], "/media/Rick.and.Morty.S09E04.zh.ass")
        self.assertEqual(str(captured["video_path"]), "/media/Rick.and.Morty.S09E04.1080p.HEVC.x265-MeGusta[EZTVx.to].mkv")
        self.assertEqual(captured["resolved_media"].season, 9)
        self.assertEqual(captured["resolved_media"].episode, 4)

    def test_job_run_video_dispatches_to_video_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp))
            job = store.create(
                "translate-video",
                {
                    "kind": "translate-video",
                    "video": {"video_path": "/media/Movie.mkv", "imdb_id": None, "title": None, "media_type": "movie"},
                    "translation": {"source_language": "en", "target_language": "zh", "output_mode": "bilingual-ass", "backend": "codex-cli", "model": "gpt-5.4-mini", "assume_unlabeled_stream_language": False},
                    "render": {"primary_script": "cjk", "secondary_script": "latin", "ass_height": 1080},
                    "output": {"output": None, "force": False, "work_dir": None, "keep_work_dir": False},
                    "provider": {"online_subtitle_fallback": False, "subtitle_provider": "all", "provider_search_limit": 10, "download_provider_priority": "opensubtitles,subdl", "allow_low_confidence_subtitle": False, "allow_provider_fallback_language": False},
                },
            )
            dispatched = {}
            parser = build_parser()
            args = parser.parse_args(["job-run", "--job-store-dir", tmp, job["job_id"]])

            def fake_video_executor(current_job, run_args):
                dispatched["kind"] = (current_job.get("request") or {}).get("kind")
                return {"output": "/media/Movie.zh.ass"}

            with patch.object(cli_module, "translate_video_job_executor", side_effect=fake_video_executor):
                summary = job_run_summary(args)

            self.assertEqual(summary["job"]["status"], "succeeded")
            self.assertEqual(dispatched["kind"], "translate-video")

    def test_cli_parser_accepts_job_create_video(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "job-create-video",
                "--video-path",
                "/media/Movie.mkv",
                "--imdb-id",
                "tt1234567",
                "--media-type",
                "episode",
                "--season",
                "9",
                "--episode",
                "4",
                "--target-language",
                "zh",
                "--no-online-subtitle-fallback",
            ]
        )
        self.assertEqual(args.command, "job-create-video")
        self.assertEqual(str(args.video_path), "/media/Movie.mkv")
        self.assertEqual(args.imdb_id, "tt1234567")
        self.assertEqual(args.media_type, "episode")
        self.assertEqual(args.season, 9)
        self.assertEqual(args.episode, 4)
        self.assertEqual(args.target_language, "zh")
        self.assertTrue(args.no_online_subtitle_fallback)


if __name__ == "__main__":
    unittest.main()
