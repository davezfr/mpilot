import os
import sys
import types
import unittest
from unittest.mock import patch

from babelarr import mcp_server


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

class McpServerTests(unittest.TestCase):
    def test_plex_search_builds_cli_args(self):
        with patch.object(mcp_server, "run_cli_summary", return_value={"status": "single_match"}) as run:
            result = mcp_server.plex_search(
                query="Project Hail Mary",
                year=2026,
                season=1,
                episode=2,
                limit=15,
                plex_base_url="http://plex.test:32400",
                plex_token="token",
                plex_path_prefix="/server/media",
                local_path_prefix="/mnt/media",
            )

        self.assertEqual(result, {"status": "single_match"})
        self.assertEqual(
            run.call_args.args[0],
            [
                "plex-search",
                "--query",
                "Project Hail Mary",
                "--year",
                "2026",
                "--season",
                "1",
                "--episode",
                "2",
                "--limit",
                "15",
                "--plex-base-url",
                "http://plex.test:32400",
                "--plex-token",
                "token",
                "--plex-path-prefix",
                "/server/media",
                "--local-path-prefix",
                "/mnt/media",
            ],
        )

    def test_subtitle_plan_builds_cli_args(self):
        with patch.object(mcp_server, "run_cli_summary", return_value={"status": "planned"}) as run:
            result = mcp_server.subtitle_plan(
                rating_key="1468",
                target_language="zh",
                preferred_source_language="en",
                plex_base_url="http://plex.test:32400",
                plex_token="token",
                plex_path_prefix="/server/media",
                local_path_prefix="/mnt/media",
            )

        self.assertEqual(result, {"status": "planned"})
        self.assertEqual(
            run.call_args.args[0],
            [
                "subtitle-plan",
                "--rating-key",
                "1468",
                "--plex-base-url",
                "http://plex.test:32400",
                "--plex-token",
                "token",
                "--plex-path-prefix",
                "/server/media",
                "--local-path-prefix",
                "/mnt/media",
                "--target-language",
                "zh",
                "--preferred-source-language",
                "en",
            ],
        )
    def test_job_create_builds_translate_plex_job_args(self):
        with patch.object(mcp_server, "run_cli_summary", return_value={"job": {"job_id": "job_123"}}) as run:
            result = mcp_server.job_create(
                imdb="tt1234567",
                season=1,
                episode=3,
                source_language="en",
                target_language="fr",
                output_mode="bilingual-ass",
                backend="fake",
                write_back=True,
                work_dir="/tmp/babelarr-job",
                job_store_dir="/tmp/babelarr-jobs",
            )

        self.assertEqual(result["job"]["job_id"], "job_123")
        self.assertEqual(
            run.call_args.args[0],
            [
                "job-create",
                "--job-store-dir",
                "/tmp/babelarr-jobs",
                "--imdb",
                "tt1234567",
                "--season",
                "1",
                "--episode",
                "3",
                "--source-language",
                "en",
                "--target-language",
                "fr",
                "--backend",
                "fake",
                "--output-mode",
                "bilingual-ass",
                "--work-dir",
                "/tmp/babelarr-job",
                "--write-back",
            ],
        )

    def test_job_create_video_accepts_provider_fallback_language_confirmation(self):
        with patch.object(mcp_server, "run_cli_summary", return_value={"job": {"job_id": "job_123"}}) as run:
            result = mcp_server.job_create_video(
                "/media/Movie.mkv",
                media_type="episode",
                season=9,
                episode=4,
                source_language="en",
                target_language="zh",
                output_mode="bilingual-ass",
                allow_provider_fallback_language=True,
                job_store_dir="/tmp/babelarr-jobs",
            )

        self.assertEqual(result["job"]["job_id"], "job_123")
        self.assertEqual(
            run.call_args.args[0],
            [
                "job-create-video",
                "--video-path",
                "/media/Movie.mkv",
                "--job-store-dir",
                "/tmp/babelarr-jobs",
                "--media-type",
                "episode",
                "--season",
                "9",
                "--episode",
                "4",
                "--source-language",
                "en",
                "--target-language",
                "zh",
                "--output-mode",
                "bilingual-ass",
                "--allow-provider-fallback-language",
            ],
        )

    def test_job_start_builds_background_job_args(self):
        with patch.object(mcp_server, "run_cli_summary", return_value={"status": "started"}) as run:
            result = mcp_server.job_start(
                "job_123",
                job_store_dir="/tmp/babelarr-jobs",
                plex_token="token",
                allow_low_confidence_subtitle=True,
                subdl_api_key="subdl-key",
            )

        self.assertEqual(result["status"], "started")
        self.assertEqual(
            run.call_args.args[0],
            [
                "job-start",
                "--job-store-dir",
                "/tmp/babelarr-jobs",
                "--plex-token",
                "token",
                "--allow-low-confidence-subtitle",
                "--subdl-api-key",
                "subdl-key",
                "job_123",
            ],
        )

    def test_job_start_returns_cli_notification_watch_without_local_notifier(self):
        payload = {
            "status": "started",
            "job_store": "/tmp/babelarr-jobs",
            "job": {
                "job_id": "job_123",
                "request": {
                    "plex": {"imdb": "tt1234567"},
                    "translation": {"target_language": "zh"},
                },
            },
            "notification_watch": {
                "status": "watching",
                "watch": {"job_id": "job_123", "notification_target": "telegram:12345"},
            },
        }
        self.assertFalse(hasattr(mcp_server, "_maybe_register_job_notification"))
        with patch.object(mcp_server, "run_cli_summary", return_value=payload) as run:
            result = mcp_server.job_start(
                "job_123",
                notification_target=None,
                requester_id="telegram:12345",
                title="The Devil Wears Prada",
            )

        self.assertEqual(result["notification_watch"]["status"], "watching")
        self.assertIn("--requester-id", run.call_args.args[0])
        self.assertIn("telegram:12345", run.call_args.args[0])
        self.assertIn("--notification-title", run.call_args.args[0])
        self.assertIn("The Devil Wears Prada", run.call_args.args[0])

    def test_job_confirm_low_confidence_starts_background_job(self):
        with patch.object(mcp_server, "job_start", return_value={"status": "started"}) as start:
            result = mcp_server.job_confirm_low_confidence(
                "job_123",
                job_store_dir="/tmp/babelarr-jobs",
                plex_token="token",
                notification_target="telegram:12345",
            )

        self.assertEqual(result["status"], "started")
        self.assertEqual(start.call_args.args, ("job_123",))
        self.assertTrue(start.call_args.kwargs["allow_low_confidence_subtitle"])
        self.assertEqual(start.call_args.kwargs["job_store_dir"], "/tmp/babelarr-jobs")
        self.assertEqual(start.call_args.kwargs["plex_token"], "token")
        self.assertEqual(start.call_args.kwargs["notification_target"], "telegram:12345")

    def test_run_cli_summary_returns_structured_errors(self):
        with patch.dict(os.environ, {"MST_NO_DOTENV": "1", "PLEX_BASE_URL": "", "PLEX_TOKEN": ""}):
            payload = mcp_server.run_cli_summary(["plex-resolve", "--rating-key", "101"])

        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error"]["type"], "PlexConfigurationError")
        self.assertIn("PLEX_BASE_URL is required", payload["error"]["message"])

    def test_create_mcp_server_registers_expected_tools(self):
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

        self.assertEqual(server.name, "babelarr")
        self.assertEqual(
            server.tool_names,
            [
                "plex_search",
                "subtitle_plan",
                "job_create",
                "job_create_video",
                "job_start",
                "job_show",
                "job_run",
                "job_resume",
                "job_confirm_low_confidence",
                "job_confirm_provider_fallback_language",
                "job_prune",
            ],
        )


if __name__ == "__main__":
    unittest.main()
