import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from mpilot.subtitles import cli as cli_module
from mpilot.subtitles.cli import build_parser, summary_from_args
from mpilot.subtitles.plex_resolver import (
    PathMapping,
    PlexApiError,
    PlexApiClient,
    PlexConfigurationError,
    PlexConnection,
    PlexNotFoundError,
    PlexResolver,
    imdb_guid,
    metadata_matches_guid,
    primary_media_file,
)


MOVIE_DETAIL = {
    "ratingKey": "101",
    "key": "/library/metadata/101",
    "librarySectionID": "1",
    "type": "movie",
    "title": "Example Movie",
    "guid": "plex://movie/example",
    "Guid": [{"id": "imdb://tt1234567"}],
    "Media": [
        {
            "Part": [
                {
                    "file": "/server/media/Movies/Example Movie.mkv",
                }
            ]
        }
    ],
}

SHOW_DETAIL = {
    "ratingKey": "201",
    "key": "/library/metadata/201",
    "type": "show",
    "title": "Example Show",
    "guid": "plex://show/example",
    "Guid": [{"id": "imdb://tt7654321"}],
}

EPISODE_DETAIL = {
    "ratingKey": "203",
    "key": "/library/metadata/203",
    "type": "episode",
    "title": "Episode Three",
    "grandparentTitle": "Example Show",
    "parentIndex": 1,
    "index": 3,
    "guid": "plex://episode/example-3",
    "Media": [
        {
            "Part": [
                {
                    "file": "/server/media/TV/Example Show/S01E03.mkv",
                }
            ]
        }
    ],
}


class FakePlexClient:
    def __init__(
        self,
        metadata_by_rating_key=None,
        search_results=None,
        sections=None,
        section_guid_results=None,
        section_items=None,
        leaves=None,
    ):
        self.metadata_by_rating_key = metadata_by_rating_key or {}
        self.search_results = search_results or []
        self.sections = sections or []
        self.section_guid_results = section_guid_results or {}
        self.section_items = section_items or {}
        self.leaves = leaves or {}

    def get_metadata(self, rating_key):
        item = self.metadata_by_rating_key.get(str(rating_key))
        return [item] if item else []

    def search(self, query, limit=50):
        return self.search_results

    def list_sections(self):
        return self.sections

    def section_items_by_guid(self, section_key, guid):
        return self.section_guid_results.get((str(section_key), guid), [])

    def section_items_with_guids(self, section_key):
        return self.section_items.get(str(section_key), [])

    def get_all_leaves(self, rating_key):
        return self.leaves.get(str(rating_key), [])


class PlexResolverTests(unittest.TestCase):
    def test_parses_media_part_file_from_metadata(self):
        self.assertEqual(
            primary_media_file(MOVIE_DETAIL),
            "/server/media/Movies/Example Movie.mkv",
        )

    def test_matches_imdb_guid(self):
        self.assertEqual(imdb_guid("https://www.imdb.com/title/tt1234567/"), "imdb://tt1234567")
        self.assertTrue(metadata_matches_guid(MOVIE_DETAIL, "imdb://tt1234567"))
        self.assertFalse(metadata_matches_guid(MOVIE_DETAIL, "imdb://tt9999999"))

    def test_resolves_movie_by_imdb_and_maps_path_prefix(self):
        client = FakePlexClient(
            metadata_by_rating_key={"101": MOVIE_DETAIL},
            search_results=[{"ratingKey": "101", "type": "movie", "title": "Example Movie"}],
        )
        resolver = PlexResolver(client, PathMapping("/server/media", "/mnt/media"))

        result = resolver.resolve(imdb="tt1234567")

        self.assertEqual(result.rating_key, "101")
        self.assertEqual(result.title, "Example Movie")
        self.assertEqual(result.media_type, "movie")
        self.assertEqual(result.plex_file, "/server/media/Movies/Example Movie.mkv")
        self.assertEqual(result.local_file, "/mnt/media/Movies/Example Movie.mkv")
        self.assertTrue(result.path_mapping_applied)
        self.assertEqual(result.imdb, "tt1234567")
        self.assertEqual(result.library_section_id, "1")

    def test_resolves_movie_by_section_guid_lookup_when_search_is_empty(self):
        client = FakePlexClient(
            metadata_by_rating_key={"101": MOVIE_DETAIL},
            sections=[{"key": "1", "type": "movie", "title": "Movies"}],
            section_guid_results={("1", "imdb://tt1234567"): [{"ratingKey": "101", "type": "movie"}]},
        )
        resolver = PlexResolver(client)

        result = resolver.resolve(imdb="tt1234567")

        self.assertEqual(result.rating_key, "101")
        self.assertEqual(result.plex_file, "/server/media/Movies/Example Movie.mkv")

    def test_resolves_movie_by_scanning_section_guids_when_guid_filter_is_empty(self):
        client = FakePlexClient(
            metadata_by_rating_key={"101": MOVIE_DETAIL},
            sections=[{"key": "1", "type": "movie", "title": "Movies"}],
            section_guid_results={("1", "imdb://tt1234567"): []},
            section_items={"1": [MOVIE_DETAIL]},
        )
        resolver = PlexResolver(client)

        result = resolver.resolve(imdb="tt1234567")

        self.assertEqual(result.rating_key, "101")
        self.assertEqual(result.imdb, "tt1234567")

    def test_resolves_tv_episode_by_show_imdb_and_season_episode(self):
        client = FakePlexClient(
            metadata_by_rating_key={"201": SHOW_DETAIL, "203": EPISODE_DETAIL},
            search_results=[{"ratingKey": "201", "type": "show", "title": "Example Show"}],
            leaves={
                "201": [
                    {"ratingKey": "202", "type": "episode", "parentIndex": 1, "index": 2},
                    {"ratingKey": "203", "type": "episode", "parentIndex": 1, "index": 3},
                ]
            },
        )
        resolver = PlexResolver(client, PathMapping("/server/media", "/mnt/media"))

        result = resolver.resolve(imdb="tt7654321", season=1, episode=3)

        self.assertEqual(result.rating_key, "203")
        self.assertEqual(result.media_type, "episode")
        self.assertEqual(result.show_title, "Example Show")
        self.assertEqual(result.imdb, "tt7654321")
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 3)
        self.assertEqual(result.local_file, "/mnt/media/TV/Example Show/S01E03.mkv")

    def test_resolves_by_rating_key(self):
        client = FakePlexClient(metadata_by_rating_key={"101": MOVIE_DETAIL})
        resolver = PlexResolver(client)

        result = resolver.resolve(rating_key="101")

        self.assertEqual(result.rating_key, "101")
        self.assertEqual(result.local_file, "/server/media/Movies/Example Movie.mkv")

    def test_searches_title_and_returns_single_playable_movie(self):
        client = FakePlexClient(
            metadata_by_rating_key={"101": MOVIE_DETAIL},
            search_results=[{"ratingKey": "101", "type": "movie", "title": "Example Movie"}],
        )
        resolver = PlexResolver(client, PathMapping("/server/media", "/mnt/media"))

        result = resolver.search_by_title("Example Movie")

        self.assertEqual(result["status"], "single_match")
        self.assertEqual(result["query"], "Example Movie")
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["ratingKey"], "101")
        self.assertEqual(result["matches"][0]["local_file"], "/mnt/media/Movies/Example Movie.mkv")

    def test_searches_title_again_after_removing_the_movie_suffix(self):
        f1_detail = dict(MOVIE_DETAIL, title="F1")

        class QueryAwarePlexClient(FakePlexClient):
            def __init__(self):
                super().__init__(metadata_by_rating_key={"101": f1_detail})
                self.queries = []

            def search(self, query, limit=50):
                self.queries.append(query)
                if query == "F1":
                    return [{"ratingKey": "101", "type": "movie", "title": "F1"}]
                return []

        client = QueryAwarePlexClient()
        resolver = PlexResolver(client, PathMapping("/server/media", "/mnt/media"))

        result = resolver.search_by_title("F1 the movie")

        self.assertEqual(result["status"], "single_match")
        self.assertEqual(result["query"], "F1 the movie")
        self.assertEqual(result["query_used"], "F1")
        self.assertEqual(result["search_strategy"], "normalized_title_fallback")
        self.assertEqual(client.queries, ["F1 the movie", "F1"])

    def test_searches_title_and_returns_multiple_playable_matches(self):
        alternate_movie = dict(MOVIE_DETAIL, ratingKey="102", title="Example Movie 2")
        client = FakePlexClient(
            metadata_by_rating_key={"101": MOVIE_DETAIL, "102": alternate_movie},
            search_results=[
                {"ratingKey": "101", "type": "movie", "title": "Example Movie"},
                {"ratingKey": "102", "type": "movie", "title": "Example Movie 2"},
            ],
        )
        resolver = PlexResolver(client)

        result = resolver.search_by_title("Example Movie")

        self.assertEqual(result["status"], "multiple_matches")
        self.assertEqual(result["match_count"], 2)
        self.assertEqual([match["ratingKey"] for match in result["matches"]], ["101", "102"])

    def test_searches_show_title_without_episode_and_requests_episode_context(self):
        client = FakePlexClient(
            metadata_by_rating_key={"201": SHOW_DETAIL},
            search_results=[{"ratingKey": "201", "type": "show", "title": "Example Show"}],
        )
        resolver = PlexResolver(client)

        result = resolver.search_by_title("Example Show")

        self.assertEqual(result["status"], "needs_episode")
        self.assertEqual(result["match_count"], 1)
        self.assertTrue(result["matches"][0]["requires_episode"])
        self.assertEqual(result["matches"][0]["ratingKey"], "201")

    def test_searches_show_title_with_episode_context_and_returns_episode(self):
        client = FakePlexClient(
            metadata_by_rating_key={"201": SHOW_DETAIL, "203": EPISODE_DETAIL},
            search_results=[{"ratingKey": "201", "type": "show", "title": "Example Show"}],
            leaves={"201": [{"ratingKey": "203", "type": "episode", "parentIndex": 1, "index": 3}]},
        )
        resolver = PlexResolver(client, PathMapping("/server/media", "/mnt/media"))

        result = resolver.search_by_title("Example Show", season=1, episode=3)

        self.assertEqual(result["status"], "single_match")
        self.assertEqual(result["matches"][0]["ratingKey"], "203")
        self.assertEqual(result["matches"][0]["show_title"], "Example Show")
        self.assertEqual(result["matches"][0]["local_file"], "/mnt/media/TV/Example Show/S01E03.mkv")

    def test_plex_search_falls_back_to_local_tv_episode_without_plex_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "TV - EN" / "Rick and Morty" / "Rick and Morty S09E03 Rick Fu Hustle 1080p AMZN WEB-DL.mkv"
            video.parent.mkdir(parents=True)
            video.write_text("video", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "BABELARR_NO_DOTENV": "1",
                    "BABELARR_LOCAL_PATH_PREFIX": str(root),
                    "BABELARR_PLEX_PATH_PREFIX": "/server/media",
                },
                clear=True,
            ):
                args = build_parser().parse_args(
                    [
                        "plex-search",
                        "--query",
                        "Rick and Morty S09E03 Kung Fu Hustle",
                        "--season",
                        "9",
                        "--episode",
                        "3",
                    ]
                )

            result = summary_from_args(args)

            self.assertEqual(result["status"], "single_match")
            self.assertEqual(result["source"], "local")
            self.assertEqual(result["matches"][0]["local_file"], str(video))
            self.assertEqual(result["matches"][0]["media_type"], "episode")
            self.assertEqual(result["matches"][0]["season"], 9)
            self.assertEqual(result["matches"][0]["episode"], 3)
            self.assertEqual(result["matches"][0]["show_title"], "Rick and Morty")

    def test_plex_search_falls_back_to_local_tv_episode_when_plex_api_fails(self):
        class FailingPlexApiClient:
            def __init__(self, _connection):
                pass

            def search(self, _query, limit=50):
                raise PlexApiError("Plex API request failed for /hubs/search: no route")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "TV" / "Rick and Morty" / "Rick.and.Morty.S09E03.1080p.WEB-DL.mkv"
            video.parent.mkdir(parents=True)
            video.write_text("video", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "BABELARR_NO_DOTENV": "1",
                    "BABELARR_LOCAL_PATH_PREFIX": str(root),
                    "BABELARR_PLEX_PATH_PREFIX": "/server/media",
                },
                clear=True,
            ):
                args = build_parser().parse_args(
                    [
                        "plex-search",
                        "--query",
                        "Rick and Morty",
                        "--season",
                        "9",
                        "--episode",
                        "3",
                        "--plex-base-url",
                        "http://plex.test:32400",
                        "--plex-token",
                        "token",
                    ]
                )

            with patch.object(cli_module, "PlexApiClient", FailingPlexApiClient):
                result = summary_from_args(args)

            self.assertEqual(result["status"], "single_match")
            self.assertEqual(result["source"], "local")
            self.assertEqual(result["matches"][0]["local_file"], str(video))

    def test_plex_search_surfaces_plex_error_when_local_fallback_has_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "BABELARR_NO_DOTENV": "1",
                    "BABELARR_LOCAL_PATH_PREFIX": tmp,
                    "BABELARR_PLEX_PATH_PREFIX": "/server/media",
                    "PLEX_BASE_URL": "",
                    "PLEX_TOKEN": "",
                },
                clear=True,
            ):
                args = build_parser().parse_args(["plex-search", "--query", "F1 the movie"])

            with self.assertRaisesRegex(PlexConfigurationError, "PLEX_BASE_URL is required"):
                summary_from_args(args)

    def test_plex_search_uses_local_tv_episode_match_before_plex_network(self):
        class UnexpectedPlexApiClient:
            def __init__(self, _connection):
                raise AssertionError("Plex should not be contacted when local TV episode matched")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "TV" / "Rick and Morty" / "Rick.and.Morty.S09E03.1080p.WEB-DL.mkv"
            video.parent.mkdir(parents=True)
            video.write_text("video", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "BABELARR_NO_DOTENV": "1",
                    "BABELARR_LOCAL_PATH_PREFIX": str(root),
                    "BABELARR_PLEX_PATH_PREFIX": "/server/media",
                },
                clear=True,
            ):
                args = build_parser().parse_args(
                    [
                        "plex-search",
                        "--query",
                        "Rick and Morty",
                        "--season",
                        "9",
                        "--episode",
                        "3",
                        "--plex-base-url",
                        "http://plex.test:32400",
                        "--plex-token",
                        "token",
                    ]
                )

            with patch.object(cli_module, "PlexApiClient", UnexpectedPlexApiClient):
                result = summary_from_args(args)

            self.assertEqual(result["status"], "single_match")
            self.assertEqual(result["source"], "local")
            self.assertEqual(result["matches"][0]["local_file"], str(video))

    def test_path_prefix_mapping_requires_both_prefixes(self):
        with self.assertRaisesRegex(PlexConfigurationError, "requires both"):
            PathMapping.from_values("/server/media", None)

    def test_path_mapping_prefers_babelarr_env_names(self):
        env = {
            "BABELARR_PLEX_PATH_PREFIX": "/server/media",
            "BABELARR_LOCAL_PATH_PREFIX": "/mnt/media",
            "MST_PLEX_PATH_PREFIX": "/legacy/server",
            "MST_LOCAL_PATH_PREFIX": "/legacy/local",
        }
        with patch.dict(os.environ, env, clear=False):
            mapping = PathMapping.from_env()

        self.assertEqual(mapping.plex_path_prefix, "/server/media")
        self.assertEqual(mapping.local_path_prefix, "/mnt/media")

    def test_not_found_has_clear_error(self):
        client = FakePlexClient()
        resolver = PlexResolver(client)

        with self.assertRaisesRegex(PlexNotFoundError, "No Plex item found for IMDb ID tt1234567"):
            resolver.resolve(imdb="tt1234567")

    def test_missing_plex_connection_values_have_clear_errors(self):
        with self.assertRaisesRegex(PlexConfigurationError, "PLEX_BASE_URL is required"):
            PlexConnection.from_values(None, "token")
        with self.assertRaisesRegex(PlexConfigurationError, "PLEX_TOKEN is required"):
            PlexConnection.from_values("http://127.0.0.1:32400", None)

    def test_plex_api_client_scans_library_path_for_sidecar_refresh(self):
        calls = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b""

        def fake_urlopen(request, timeout):
            calls.append({"url": request.full_url, "headers": dict(request.header_items()), "timeout": timeout})
            return FakeResponse()

        client = PlexApiClient(PlexConnection(base_url="http://plex.test:32400", token="token", timeout=7))

        with patch("urllib.request.urlopen", fake_urlopen):
            response = client.scan_library_path("1", "/server/media/Movies/Example Movie")

        parsed = urllib.parse.urlparse(calls[0]["url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/library/sections/1/refresh")
        self.assertNotIn("X-Plex-Token", query)
        self.assertEqual(query["path"], ["/server/media/Movies/Example Movie"])
        headers = {key.lower(): value for key, value in calls[0]["headers"].items()}
        self.assertEqual(headers["x-plex-token"], "token")
        self.assertEqual(calls[0]["timeout"], 7)
        self.assertEqual(response["status"], "requested")
        self.assertEqual(response["path"], "/server/media/Movies/Example Movie")

    def test_cli_reports_missing_plex_config_before_network_call(self):
        env = dict(os.environ)
        env.pop("PLEX_BASE_URL", None)
        env.pop("PLEX_TOKEN", None)
        env["MST_NO_DOTENV"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "mpilot.subtitles", "plex-resolve", "--rating-key", "101"],
            text=True,
            capture_output=True,
            env=env,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PLEX_BASE_URL is required", result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["type"], "PlexConfigurationError")


if __name__ == "__main__":
    unittest.main()
