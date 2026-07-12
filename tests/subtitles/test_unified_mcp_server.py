from __future__ import annotations

import asyncio
import inspect
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from mpilot.mcp import server as unified
from mpilot.runtime import RuntimeStoreError


class FakeFastMCP:
    def __init__(self, name, instructions=None):
        self.name = name
        self.instructions = instructions
        self.tool_names = []
        self.tool_docs = {}
        self.tool_functions = {}

    def tool(self):
        def decorator(func):
            self.tool_names.append(func.__name__)
            self.tool_docs[func.__name__] = func.__doc__ or ""
            self.tool_functions[func.__name__] = func
            return func

        return decorator


class FakeNotifier:
    class Store:
        def pending_watches(self):
            return []

    def __init__(self):
        self.store = self.Store()
        self.start_count = 0
        self.register_calls = []

    async def register_watch(self, **kwargs):
        self.register_calls.append(kwargs)
        return {"info_hash": kwargs["info_hash"], "notification_target": kwargs["notification_target"]}

    def start(self):
        self.start_count += 1
        return None


class FakeAcquisitionClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def handle(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.payload)

    async def complementary_search(self, query_id):
        self.calls.append({"query_id": query_id})
        return dict(self.payload)


class UnifiedMcpServerTests(unittest.TestCase):
    def test_create_mcp_server_registers_full_tool_union_when_configured(self):
        server = _create_server_with_env(
            {
                "QBITLARR_API_URL": "http://127.0.0.1:1",
                "PLEX_BASE_URL": "http://plex.test:32400",
                "PLEX_TOKEN": "token",
                "MPILOT_ENABLE_RUNTIME_OPERATOR_TOOLS": "true",
                "MPILOT_ENABLE_ACQUISITION_CONTROL_TOOLS": "true",
            }
        )

        self.assertEqual(server.name, "mpilot")
        self.assertTrue(
            {
                "acquisition_handle",
                "acquisition_complementary_search",
                "acquisition_download",
                "job_create",
                "job_start",
                "plex_search",
                "subtitle_plan",
                "record_acquisition_download_with_subtitle_intent",
                "claim_ready_subtitle_job_create_video_actions",
                "record_subtitle_job_created",
                "record_subtitle_job_status",
                "queue_status",
                "media_request",
            }.issubset(set(server.tool_names))
        )
        self.assertNotIn("claim_ready_babelarr_job_create_video_actions", server.tool_names)
        self.assertNotIn("record_babelarr_job_created", server.tool_names)
        self.assertNotIn("record_babelarr_job_status", server.tool_names)
        self.assertNotIn("qbitlarr_handle", server.tool_names)
        self.assertNotIn("qbitlarr_download", server.tool_names)
        self.assertNotIn("record_qbitlarr_download_with_subtitle_intent", server.tool_names)
        self.assertIn("agent_clarify.display_table", server.tool_docs["acquisition_handle"])
        self.assertIn("acquisition_download", server.tool_docs["acquisition_handle"])
        self.assertIn("qbot protocol", server.tool_docs["acquisition_complementary_search"])
        self.assertIn("补充搜索", server.tool_docs["acquisition_complementary_search"])
        self.assertIn("there is no active query_id", server.tool_docs["acquisition_complementary_search"])
        self.assertIn("Never", server.tool_docs["acquisition_complementary_search"])
        self.assertIn("rendered_status", server.tool_docs["acquisition_download"])
        self.assertIn("requester_id is required", server.tool_docs["acquisition_delete_download"])

    def test_create_mcp_server_can_mount_download_tools_without_subtitle_tools(self):
        server = _create_server_with_env({"QBITLARR_API_URL": "http://127.0.0.1:1"})

        self.assertIn("acquisition_handle", server.tool_names)
        self.assertIn("acquisition_complementary_search", server.tool_names)
        self.assertIn("media_request", server.tool_names)
        self.assertNotIn("queue_status", server.tool_names)
        self.assertNotIn("workflow_show", server.tool_names)
        self.assertNotIn("acquisition_delete_download", server.tool_names)
        self.assertNotIn("job_create", server.tool_names)
        self.assertNotIn("plex_search", server.tool_names)

    def test_create_mcp_server_can_mount_subtitle_tools_without_download_tools(self):
        server = _create_server_with_env({"PLEX_BASE_URL": "http://plex.test:32400", "PLEX_TOKEN": "token"})

        self.assertIn("job_create", server.tool_names)
        self.assertIn("subtitle_plan", server.tool_names)
        self.assertNotIn("queue_status", server.tool_names)
        self.assertNotIn("media_request", server.tool_names)
        self.assertNotIn("acquisition_handle", server.tool_names)

    def test_complementary_search_tool_prepares_numeric_clarify_choices(self):
        client = FakeAcquisitionClient(
            {
                "status": "success",
                "action": "show_results",
                "results_verified_by_imdb_id": False,
                "message": "Not IMDb-verified.",
                "choice_buttons": [{"index": 1, "text": "1", "value": "1"}],
                "results": [
                    {
                        "index": 1,
                        "title": "Sarajevo.Safari.2022.1080p.HDTV.x264",
                        "quality": "1080p HDTV H.264",
                        "seeders": 1,
                        "size": 3_100_000_000,
                        "download_link": "https://example.test/1.torrent",
                        "indexer": "RuTracker",
                    }
                ],
            }
        )
        server = FakeFastMCP("mpilot")

        with patch.object(unified, "get_acquisition_client", return_value=client):
            unified._register_acquisition_tools(server, FakeNotifier())
            payload = asyncio.run(server.tool_functions["acquisition_complementary_search"]("query-123"))

        self.assertEqual(client.calls, [{"query_id": "query-123"}])
        self.assertEqual(payload["agent_clarify"]["choices"], ["1"])
        self.assertIn("Sarajevo.Safari.2022.1080p.HDTV.x264", payload["agent_clarify"]["display_table"])
        self.assertIn("RuTracker", payload["agent_clarify"]["display_table"])
        self.assertNotIn("choice_buttons", payload)

    def test_operator_and_destructive_tools_require_explicit_opt_in(self):
        server = _create_server_with_env(
            {
                "QBITLARR_API_URL": "http://127.0.0.1:1",
                "MPILOT_ENABLE_RUNTIME_OPERATOR_TOOLS": "true",
                "MPILOT_ENABLE_ACQUISITION_CONTROL_TOOLS": "true",
            }
        )

        self.assertIn("queue_status", server.tool_names)
        self.assertIn("workflow_show", server.tool_names)
        self.assertIn("list_workflows", server.tool_names)
        self.assertIn("acquisition_pause_download", server.tool_names)
        self.assertIn("acquisition_resume_download", server.tool_names)
        self.assertIn("acquisition_delete_download", server.tool_names)

    def test_media_request_without_subtitles_is_qbitlarr_handle_passthrough(self):
        client = FakeAcquisitionClient(
            {
                "status": "success",
                "action": "auto_download",
                "title": "Example Movie",
                "download_status": {"hash": "abc123", "name": "Example.Movie.mkv", "progress": 0.25},
            }
        )

        with patch.object(unified, "get_acquisition_client", return_value=client):
            payload = asyncio.run(unified.media_request("Example Movie", requester_id="user-123", mode="auto"))

        self.assertEqual(payload["action"], "auto_download")
        self.assertNotIn("subtitle_intent", payload)
        self.assertEqual(client.calls[0]["user_message"], "Example Movie")
        self.assertEqual(client.calls[0]["user_id"], "user-123")
        self.assertEqual(client.calls[0]["mode"], "auto")

    def test_media_request_records_download_and_subtitle_intent(self):
        client = FakeAcquisitionClient(
            {
                "status": "success",
                "action": "auto_download",
                "title": "Example Movie",
                "imdb_id": "tt1234567",
                "media_type": "movie",
                "download_status": {
                    "hash": "abc123",
                    "name": "Example.Movie.mkv",
                    "progress": 0.25,
                    "content_path": None,
                },
            }
        )
        calls = []

        def fake_record(**kwargs):
            calls.append(kwargs)
            return {"workflow_id": "workflow_123", "tasks": [{"task_type": "download_media"}]}

        with patch.object(unified, "get_acquisition_client", return_value=client), patch.object(
            unified.runtime_tools,
            "record_qbitlarr_download_with_subtitle_intent",
            side_effect=fake_record,
        ):
            payload = asyncio.run(
                unified.media_request(
                    "Example Movie",
                    requester_id="user-123",
                    subtitle_target_language="zh",
                    subtitle_source_language="en",
                    output_mode="bilingual-ass",
                    mode="auto",
                )
            )

        self.assertEqual(payload["subtitle_intent"]["status"], "registered")
        self.assertEqual(payload["subtitle_intent"]["workflow_id"], "workflow_123")
        self.assertEqual(calls[0]["requester_id"], "user-123")
        self.assertEqual(calls[0]["info_hash"], "abc123")
        self.assertEqual(calls[0]["target_language"], "zh")
        self.assertEqual(calls[0]["output_mode"], "bilingual-ass")
        self.assertEqual(calls[0]["imdb_id"], "tt1234567")
        self.assertNotIn("runtime_store_dir", calls[0])

    def test_media_request_composes_with_real_runtime_tool_contract(self):
        client = FakeAcquisitionClient(
            {
                "status": "success",
                "action": "auto_download",
                "title": "Example Movie",
                "download_status": {"hash": "abc123", "name": "Example.Movie.mkv", "progress": 0.25},
            }
        )

        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"MPILOT_RUNTIME_STORE_DIR": tmp},
            clear=False,
        ), patch.object(unified, "get_acquisition_client", return_value=client):
            payload = asyncio.run(
                unified.media_request(
                    "Example Movie",
                    requester_id="user-123",
                    subtitle_target_language="zh",
                    mode="auto",
                )
            )
            workflow_files = list(Path(tmp).glob("workflow_*.json"))

        self.assertEqual(payload["subtitle_intent"]["status"], "registered")
        self.assertEqual(len(workflow_files), 1)
        self.assertNotIn("runtime_store_dir", inspect.signature(unified.media_request).parameters)

    def test_media_request_carries_subtitle_intent_through_clarify_payload(self):
        client = FakeAcquisitionClient(
            {
                "status": "success",
                "action": "show_results",
                "message": "Choose one",
                "results": [
                    {
                        "index": 1,
                        "title": "Example.2026.1080p.WEB-DL.H.264-GRP",
                        "quality": "1080p WEB-DL H.264",
                        "label": "WEB-DL H.264",
                        "seeders": 50,
                        "size": 1_000_000_000,
                        "download_link": "https://example.test/1.torrent",
                    }
                ],
            }
        )

        with patch.object(unified, "get_acquisition_client", return_value=client):
            payload = asyncio.run(
                unified.media_request(
                    "Example Movie",
                    requester_id="user-123",
                    subtitle_target_language="zh",
                )
            )

        self.assertEqual(payload["action"], "show_results")
        self.assertIn("agent_clarify", payload)
        self.assertEqual(payload["subtitle_intent"]["status"], "pending_user_selection")
        self.assertEqual(payload["subtitle_intent"]["continue_with"]["tool"], "media_request")
        self.assertEqual(
            payload["subtitle_intent"]["continue_with"]["arguments"]["subtitle_target_language"],
            "zh",
        )
        self.assertNotIn("runtime_store_dir", payload["subtitle_intent"]["continue_with"]["arguments"])

    def test_media_request_keeps_download_payload_when_runtime_registration_fails(self):
        client = FakeAcquisitionClient(
            {
                "status": "success",
                "action": "auto_download",
                "title": "Example Movie",
                "download_status": {"hash": "abc123", "progress": 0.25},
            }
        )

        def fake_record(**_kwargs):
            raise RuntimeStoreError("store is unavailable")

        with patch.object(unified, "get_acquisition_client", return_value=client), patch.object(
            unified.runtime_tools,
            "record_qbitlarr_download_with_subtitle_intent",
            side_effect=fake_record,
        ):
            payload = asyncio.run(
                unified.media_request(
                    "Example Movie",
                    requester_id="user-123",
                    subtitle_target_language="zh",
                    mode="auto",
                )
            )

        self.assertEqual(payload["action"], "auto_download")
        self.assertEqual(payload["subtitle_intent"]["status"], "registration_failed")
        self.assertEqual(payload["subtitle_intent"]["error"]["type"], "RuntimeStoreError")

    def test_media_request_reuses_single_download_completion_notifier(self):
        client = FakeAcquisitionClient(
            {
                "status": "success",
                "action": "auto_download",
                "title": "Example Movie",
                "download_status": {"hash": "abc123", "name": "Example.Movie.mkv", "progress": 0.25},
            }
        )
        notifier = FakeNotifier()

        with patch.object(unified, "_download_completion_notifier_cache", None), patch.object(
            unified,
            "get_acquisition_client",
            return_value=client,
        ), patch.object(
            unified.DownloadCompletionNotifier,
            "from_env",
            return_value=notifier,
        ) as from_env:
            asyncio.run(unified.media_request("Example Movie", requester_id="user-123"))
            asyncio.run(unified.media_request("Example Movie", notification_target="telegram:123"))
            asyncio.run(unified.media_request("Example Movie", notification_target="telegram:123"))

        from_env.assert_called_once()
        self.assertEqual(len(notifier.register_calls), 2)


def _create_server_with_env(env):
    fake_fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    fake_fastmcp_module.FastMCP = FakeFastMCP

    with patch.dict(os.environ, env, clear=True), patch.dict(
        sys.modules,
        {
            "mcp": types.ModuleType("mcp"),
            "mcp.server": types.ModuleType("mcp.server"),
            "mcp.server.fastmcp": fake_fastmcp_module,
        },
    ), patch.object(unified, "_download_completion_notifier_cache", None), patch.object(
        unified.DownloadCompletionNotifier,
        "from_env",
        return_value=FakeNotifier(),
    ):
        return unified.create_mcp_server()


if __name__ == "__main__":
    unittest.main()
