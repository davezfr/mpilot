from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace

from mpilot.acquisition.cli import main
from mpilot.acquisition.client import AcquisitionApiError


class FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def handle(self, user_message, user_id=None, save_path=None, mode=None):
        self.calls.append(
            (
                "handle",
                {
                    "user_message": user_message,
                    "user_id": user_id,
                    "save_path": save_path,
                    "mode": mode,
                },
            )
        )
        return {
            "status": "success",
            "action": "auto_download",
            "title": "The Hitch-Hiker (1953)",
            "quality": "1080p WEB-DL H.264",
            "message": (
                "The Hitch-Hiker (1953) is now downloading with 9 seeders. "
                "You can ask for a status update any time."
            ),
        }

    async def search(self, *, identifier=None, query=None, categories=None, indexer_ids=None):
        self.calls.append(
            (
                "search",
                {
                    "identifier": identifier,
                    "query": query,
                    "categories": categories,
                    "indexer_ids": indexer_ids,
                },
            )
        )
        return [{"title": "The.General.1926.1080p.WEB-DL.H.264-GRP", "seeders": 42}]

    async def download(self, download_link, save_path=None, query_id=None, user_id=None):
        self.calls.append(
            (
                "download",
                {
                    "download_link": download_link,
                    "save_path": save_path,
                    "query_id": query_id,
                    "user_id": user_id,
                },
            )
        )
        return {"status": "success", "message": "Download queued"}

    async def list_downloads(self, user_id=None):
        self.calls.append(("downloads", {"user_id": user_id}))
        return [{"name": "Ubuntu 24.04", "state": "downloading", "progress": 0.5}]

    async def get_download_status(self, info_hash, user_id=None):
        self.calls.append(("download-status", {"info_hash": info_hash, "user_id": user_id}))
        return {"name": "Ubuntu 24.04", "state": "downloading", "progress": 0.5, "hash": info_hash}

    async def render_downloads_status(self, user_id=None):
        self.calls.append(("downloads-render", {"user_id": user_id}))
        return {
            "message": "⬇️ Ubuntu 24.04\n🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50%",
            "watch_policy": {"max_duration_seconds": 900},
            "downloads": [],
        }

    async def render_download_status(self, info_hash, user_id=None):
        self.calls.append(("download-status-render", {"info_hash": info_hash, "user_id": user_id}))
        return {
            "message": "⬇️ Ubuntu 24.04\n🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50%",
            "watch_policy": {"max_duration_seconds": 900},
            "download": {"hash": info_hash},
        }

    async def health(self, *, deep=False):
        self.calls.append(("health", {"deep": deep}))
        return {"status": "ok", "service": "MPilot acquisition API"}

    async def list_prowlarr_indexers(self):
        self.calls.append(("indexers", {}))
        return [{"id": 10, "name": "Trusted Indexer", "enabled": True}]

    async def get_query_snapshot(self, query_id):
        self.calls.append(("snapshot", {"query_id": query_id}))
        return {"query_id": query_id, "status": "complementary_ready"}


def _run_cli(argv, fake_client: FakeClient):
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        argv,
        stdout=stdout,
        stderr=stderr,
        client_factory=lambda args: fake_client,
    )

    return SimpleNamespace(
        exit_code=exit_code,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


def test_cli_handle_prints_auto_download_message_by_default():
    fake_client = FakeClient()

    result = _run_cli(["handle", "tt0045877"], fake_client)

    assert result.exit_code == 0
    assert result.stdout == (
        "The Hitch-Hiker (1953) is now downloading with 9 seeders. "
        "You can ask for a status update any time.\n"
    )
    assert fake_client.calls == [
        (
            "handle",
            {
                "user_message": "tt0045877",
                "user_id": None,
                "save_path": None,
                "mode": None,
            },
        )
    ]


def test_cli_handle_prints_numbered_results_by_default():
    class ManualResultsClient(FakeClient):
        async def handle(self, user_message, user_id=None, save_path=None, mode=None):
            self.calls.append(
                (
                    "handle",
                    {
                        "user_message": user_message,
                        "user_id": user_id,
                        "save_path": save_path,
                        "mode": mode,
                    },
                )
            )
            return {
                "status": "success",
                "action": "show_results",
                "message": "Here are the top results, please reply with the number:",
                "results": [
                    {
                        "index": 1,
                        "title": "The.General.1926.1080p.WEB-DL.H.264-GRP",
                        "quality": "1080p WEB-DL H.264",
                        "seeders": 42,
                        "size": 1_500_000_000,
                        "download_link": "https://example.test/the-general.torrent",
                    },
                    {
                        "index": 2,
                        "title": "The.General.1926.720p.WEB-DL.H.264-GRP",
                        "quality": "720p WEB-DL H.264",
                        "seeders": 9,
                        "size": None,
                        "download_link": "https://example.test/the-general-720p.torrent",
                    },
                ],
            }

    result = _run_cli(["handle", "The General"], ManualResultsClient())

    assert result.exit_code == 0
    assert result.stdout == (
        "Here are the top results, please reply with the number:\n"
        "\n"
        "1. The.General.1926.1080p.WEB-DL.H.264-GRP\n"
        "   Quality: 1080p WEB-DL H.264 | Seeders: 42 | Size: 1.5 GB\n"
        "2. The.General.1926.720p.WEB-DL.H.264-GRP\n"
        "   Quality: 720p WEB-DL H.264 | Seeders: 9\n"
        "\n"
        "Use --json to inspect download links or pass a chosen link to `mpilot acquisition download`.\n"
    )
    assert "download_link" not in result.stdout


def test_cli_handle_prints_title_candidates_for_choose_title():
    class ChooseTitleClient(FakeClient):
        async def handle(self, user_message, user_id=None, save_path=None, mode=None):
            return {
                "status": "success",
                "action": "choose_title",
                "message": "I found a few possible matches. Reply with the number of the title you mean:",
                "candidates": [
                    {"index": 1, "title": "The Hitch-Hiker", "year": 1953, "imdb_id": "tt0045877", "label": "The Hitch-Hiker (1953)"},
                    {"index": 2, "title": "The Hitch Hiker", "year": 2004, "imdb_id": "tt0430185", "label": "The Hitch Hiker (2004)"},
                ],
            }

    result = _run_cli(["handle", "The Hitch-Hiker"], ChooseTitleClient())

    assert result.exit_code == 0
    assert "1. The Hitch-Hiker (1953)" in result.stdout
    assert "2. The Hitch Hiker (2004)" in result.stdout
    assert "mpilot acquisition handle tt0045877" in result.stdout


def test_cli_handle_json_flag_forwards_message_mode_and_save_path():
    fake_client = FakeClient()

    result = _run_cli(
        ["handle", "The", "Hitch-Hiker", "--mode", "manual", "--save-path", "/media/Kids", "--json"],
        fake_client,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["title"] == "The Hitch-Hiker (1953)"
    assert fake_client.calls == [
        (
            "handle",
            {
                "user_message": "The Hitch-Hiker",
                "user_id": None,
                "save_path": "/media/Kids",
                "mode": "manual",
            },
        )
    ]


def test_cli_search_prints_json_for_jq():
    fake_client = FakeClient()

    result = _run_cli(["search", "--query", "The General 1926 1080p"], fake_client)

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["seeders"] == 42
    assert fake_client.calls == [
        (
            "search",
            {
                "identifier": None,
                "query": "The General 1926 1080p",
                "categories": None,
                "indexer_ids": None,
            },
        )
    ]


def test_cli_search_forwards_categories_and_indexer_ids():
    fake_client = FakeClient()

    result = _run_cli(
        [
            "search",
            "--query",
            "The General 1926",
            "--category",
            "2000",
            "--category",
            "2040",
            "--indexer-id",
            "10",
        ],
        fake_client,
    )

    assert result.exit_code == 0
    assert fake_client.calls == [
        (
            "search",
            {
                "identifier": None,
                "query": "The General 1926",
                "categories": [2000, 2040],
                "indexer_ids": [10],
            },
        )
    ]


def test_cli_download_forwards_link_and_save_path():
    fake_client = FakeClient()

    result = _run_cli(
        ["download", "magnet:?xt=urn:btih:abcdef", "--save-path", "/media/Kids"],
        fake_client,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["message"] == "Download queued"
    assert fake_client.calls == [
        (
            "download",
            {
                "download_link": "magnet:?xt=urn:btih:abcdef",
                "save_path": "/media/Kids",
                "query_id": None,
                "user_id": None,
            },
        )
    ]


def test_cli_download_can_forward_query_id():
    fake_client = FakeClient()

    result = _run_cli(
        ["download", "magnet:?xt=urn:btih:abcdef", "--query-id", "query-123"],
        fake_client,
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["message"] == "Download queued"
    assert fake_client.calls == [
        (
            "download",
            {
                "download_link": "magnet:?xt=urn:btih:abcdef",
                "save_path": None,
                "query_id": "query-123",
                "user_id": None,
            },
        )
    ]


def test_cli_downloads_lists_torrents():
    fake_client = FakeClient()

    result = _run_cli(["downloads"], fake_client)

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["name"] == "Ubuntu 24.04"
    assert fake_client.calls == [("downloads", {"user_id": None})]


def test_cli_download_status_reads_single_torrent():
    fake_client = FakeClient()

    result = _run_cli(["download-status", "abcdef1234567890"], fake_client)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["hash"] == "abcdef1234567890"
    assert fake_client.calls == [("download-status", {"info_hash": "abcdef1234567890", "user_id": None})]


def test_cli_downloads_can_filter_by_user_id():
    fake_client = FakeClient()

    result = _run_cli(["downloads", "--user-id", "telegram:123456789"], fake_client)

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["name"] == "Ubuntu 24.04"
    assert fake_client.calls == [("downloads", {"user_id": "telegram:123456789"})]


def test_cli_downloads_render_prints_chat_status_text():
    fake_client = FakeClient()

    result = _run_cli(["downloads", "--render", "--user-id", "telegram:123456789"], fake_client)

    assert result.exit_code == 0
    assert result.stdout == "⬇️ Ubuntu 24.04\n🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50%\n"
    assert fake_client.calls == [("downloads-render", {"user_id": "telegram:123456789"})]


def test_cli_health_supports_deep_check():
    fake_client = FakeClient()

    result = _run_cli(["health", "--deep"], fake_client)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["service"] == "MPilot acquisition API"
    assert fake_client.calls == [("health", {"deep": True})]


def test_cli_indexers_lists_prowlarr_indexers():
    fake_client = FakeClient()

    result = _run_cli(["indexers"], fake_client)

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["id"] == 10
    assert fake_client.calls == [("indexers", {})]


def test_cli_snapshot_reads_saved_query_snapshot():
    fake_client = FakeClient()

    result = _run_cli(["snapshot", "query-123"], fake_client)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "complementary_ready"
    assert fake_client.calls == [("snapshot", {"query_id": "query-123"})]


def test_cli_download_status_render_prints_chat_status_text():
    fake_client = FakeClient()

    result = _run_cli(["download-status", "abcdef1234567890", "--render"], fake_client)

    assert result.exit_code == 0
    assert result.stdout == "⬇️ Ubuntu 24.04\n🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50%\n"
    assert fake_client.calls == [
        ("download-status-render", {"info_hash": "abcdef1234567890", "user_id": None})
    ]


def test_cli_search_requires_identifier_or_query():
    fake_client = FakeClient()

    result = _run_cli(["search"], fake_client)

    assert result.exit_code == 1
    assert "search requires --identifier, --query, or both" in result.stderr
    assert fake_client.calls == []


def test_cli_api_errors_are_printed_to_stderr():
    class FailingClient(FakeClient):
        async def health(self, *, deep=False):
            raise AcquisitionApiError("MPilot acquisition API is unreachable: ConnectError")

    result = _run_cli(["health"], FailingClient())

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "MPilot acquisition API is unreachable: ConnectError" in result.stderr


def test_legacy_qbitlarr_launcher_is_not_shipped():
    script_path = "bin/" + "qbitlarr"

    assert not os.path.exists(script_path)
