import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from babelarr import cli as cli_module
from babelarr.cli import build_parser, build_plex_online_subtitle_fetcher, subtitle_fetch_summary, subtitle_search_summary
from babelarr.plex_resolver import PlexResolvedMedia
from babelarr.provider_policy import (
    LowConfidenceSubtitleCandidatesError,
    download_first_provider_candidate,
    provider_names_for_search,
    rank_candidates_for_download,
)
from babelarr.providers.base import (
    DownloadedSubtitle,
    SubtitleProviderApiError,
    SubtitleProviderConfigurationError,
    SubtitleCandidate,
    SubtitleSearchRequest,
)
from babelarr.providers.opensubtitles import OpenSubtitlesConfig, OpenSubtitlesLoginResult, OpenSubtitlesProvider
from babelarr.providers.subdl import SubDLConfig, SubDLProvider


class FakeHttpGet:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, params, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return self.payload


class FakeHttpPost:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "body": dict(body),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return self.payload


class FakeHttpPostSequence:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "body": dict(body),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return self.payloads.pop(0)


class FakeHttpDownload:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return self.payload


class FakeDownloadProvider:
    def __init__(self, name, error=None):
        self.name = name
        self.error = error
        self.calls = []

    def download(self, candidate, output_dir, force=False, target_season=None, target_episode=None):
        self.calls.append(
            {
                "candidate": candidate,
                "output_dir": output_dir,
                "force": force,
                "target_season": target_season,
                "target_episode": target_episode,
            }
        )
        if self.error:
            raise self.error
        return DownloadedSubtitle(
            provider=self.name,
            path=output_dir / (candidate.file_name or "%s.srt" % self.name),
            source_url=(candidate.download or {}).get("url"),
        )


class FakeSearchDownloadProvider(FakeDownloadProvider):
    def __init__(self, name, candidates, error=None):
        super().__init__(name, error=error)
        self.candidates = candidates
        self.search_calls = []

    def search(self, request):
        self.search_calls.append(request)
        return list(self.candidates)


class LimitAwareSearchDownloadProvider(FakeSearchDownloadProvider):
    def search(self, request):
        self.search_calls.append(request)
        return list(self.candidates[: request.limit])


class LowConfidenceSubtitleContractTests(unittest.TestCase):
    def test_low_confidence_confirmation_payload_is_language_neutral(self):
        payload = LowConfidenceSubtitleCandidatesError([], [], "medium").to_dict()

        self.assertEqual(payload["action"], "confirm_low_confidence_subtitle")
        self.assertEqual(payload["confirmation_reason"], "low_confidence_match")
        self.assertEqual(payload["message_key"], "low_confidence_subtitle_confirmation")
        self.assertIn("may not match the video timeline", payload["message"])
        self.assertNotRegex(payload["message"], r"[\u4e00-\u9fff]")


def zip_payload(filename, text):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(filename, text)
    return buffer.getvalue()


def zip_payload_files(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for filename, text in files:
            archive.writestr(filename, text)
    return buffer.getvalue()


class SubtitleProviderTests(unittest.TestCase):
    def test_opensubtitles_searches_by_imdb_and_parses_candidates(self):
        http = FakeHttpGet(
            {
                "data": [
                    {
                        "id": "os-1",
                        "attributes": {
                            "language": "en",
                            "release": "Inception.2010.1080p.BluRay",
                            "hearing_impaired": False,
                            "download_count": 42,
                            "feature_details": {"title": "Inception", "year": 2010, "imdb_id": 1375666},
                            "files": [{"file_id": 2712566, "file_name": "Inception.2010.en.srt"}],
                        },
                    }
                ]
            }
        )
        provider = OpenSubtitlesProvider(
            OpenSubtitlesConfig(api_key="os-key", user_agent="babelarr v0.1"),
            http_get=http,
        )

        results = provider.search(
            SubtitleSearchRequest(
                media_type="movie",
                imdb_id="tt1375666",
                languages=("en",),
                limit=5,
            )
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(http.calls[0]["url"], "https://api.opensubtitles.com/api/v1/subtitles")
        self.assertEqual(http.calls[0]["params"]["imdb_id"], "1375666")
        self.assertEqual(http.calls[0]["params"]["languages"], "en")
        self.assertEqual(http.calls[0]["params"]["type"], "movie")
        self.assertEqual(http.calls[0]["params"]["page"], "1")
        self.assertEqual(http.calls[0]["headers"]["Api-Key"], "os-key")
        self.assertEqual(http.calls[0]["headers"]["User-Agent"], "babelarr v0.1")
        self.assertEqual(http.calls[0]["headers"]["X-User-Agent"], "babelarr v0.1")
        self.assertEqual(results[0].provider, "opensubtitles")
        self.assertEqual(results[0].provider_id, "os-1")
        self.assertEqual(results[0].language, "en")
        self.assertEqual(results[0].release_name, "Inception.2010.1080p.BluRay")
        self.assertEqual(results[0].file_name, "Inception.2010.en.srt")
        self.assertEqual(results[0].file_id, "2712566")
        self.assertEqual(results[0].download["method"], "opensubtitles-download")

    def test_opensubtitles_searches_tv_episode_with_season_episode(self):
        http = FakeHttpGet({"data": []})
        provider = OpenSubtitlesProvider(
            OpenSubtitlesConfig(api_key="os-key", user_agent="babelarr v0.1"),
            http_get=http,
        )

        provider.search(
            SubtitleSearchRequest(
                media_type="episode",
                title="Example Show",
                season=1,
                episode=3,
                languages=("fr",),
            )
        )

        self.assertEqual(http.calls[0]["params"]["query"], "Example Show")
        self.assertEqual(http.calls[0]["params"]["season_number"], "1")
        self.assertEqual(http.calls[0]["params"]["episode_number"], "3")
        self.assertEqual(http.calls[0]["params"]["languages"], "fr")
        self.assertEqual(http.calls[0]["params"]["type"], "episode")

    def test_opensubtitles_config_accepts_username_password_for_login(self):
        config = OpenSubtitlesConfig.from_values(
            api_key="os-key",
            username="opensub-user",
            password="opensub-password",
        )

        self.assertEqual(config.username, "opensub-user")
        self.assertEqual(config.password, "opensub-password")

    def test_opensubtitles_config_requires_login_credentials_or_token(self):
        with self.assertRaisesRegex(SubtitleProviderConfigurationError, "OPENSUBTITLES_USERNAME is required"):
            OpenSubtitlesConfig.from_values(api_key="os-key")

        config = OpenSubtitlesConfig.from_values(api_key="os-key", token="jwt-token")

        self.assertEqual(config.token, "jwt-token")

    def test_opensubtitles_login_uses_account_credentials_and_api_key(self):
        http_post = FakeHttpPost(
            {
                "token": "jwt-token",
                "base_url": "vip-api.opensubtitles.com",
                "user": {"allowed_downloads": 100},
            }
        )
        provider = OpenSubtitlesProvider(
            OpenSubtitlesConfig.from_values(
                api_key="os-key",
                user_agent="babelarr v0.1",
                username="opensub-user",
                password="opensub-password",
            ),
            http_get=FakeHttpGet({"data": []}),
            http_post=http_post,
        )

        result = provider.login()

        self.assertEqual(result, OpenSubtitlesLoginResult(token="jwt-token", base_url="https://vip-api.opensubtitles.com/api/v1"))
        self.assertEqual(http_post.calls[0]["url"], "https://api.opensubtitles.com/api/v1/login")
        self.assertEqual(http_post.calls[0]["body"], {"username": "opensub-user", "password": "opensub-password"})
        self.assertEqual(http_post.calls[0]["headers"]["Api-Key"], "os-key")
        self.assertEqual(http_post.calls[0]["headers"]["User-Agent"], "babelarr v0.1")
        self.assertEqual(http_post.calls[0]["headers"]["X-User-Agent"], "babelarr v0.1")
        self.assertEqual(http_post.calls[0]["headers"]["Content-Type"], "application/json")

    def test_opensubtitles_download_logs_in_requests_link_and_saves_file(self):
        http_post = FakeHttpPostSequence(
            [
                {"token": "jwt-token", "base_url": "api.opensubtitles.com"},
                {"link": "https://download.opensubtitles.test/file.srt", "file_name": "Inception.2010.en.srt"},
            ]
        )
        http_download = FakeHttpDownload(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
        provider = OpenSubtitlesProvider(
            OpenSubtitlesConfig.from_values(
                api_key="os-key",
                username="opensub-user",
                password="opensub-password",
            ),
            http_post=http_post,
            http_download=http_download,
        )
        candidate = SubtitleCandidate(
            provider="opensubtitles",
            provider_id="os-1",
            language="en",
            file_id="2712566",
            file_name="Inception.2010.en.srt",
            download={"method": "opensubtitles-download", "file_id": "2712566", "requires_token": True},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp))

            self.assertEqual(result.provider, "opensubtitles")
            self.assertEqual(result.path.name, "Inception.2010.en.srt")
            self.assertEqual(result.path.read_text(encoding="utf-8"), "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
            self.assertFalse(result.extracted_from_archive)
            self.assertEqual(http_post.calls[1]["url"], "https://api.opensubtitles.com/api/v1/download")
            self.assertEqual(http_post.calls[1]["body"], {"file_id": 2712566})
            self.assertEqual(http_post.calls[1]["headers"]["Authorization"], "Bearer jwt-token")
            self.assertEqual(http_download.calls[0]["url"], "https://download.opensubtitles.test/file.srt")
            self.assertEqual(http_download.calls[0]["headers"]["User-Agent"], "MediaSubtitleTranslator v0.1.0")

    def test_opensubtitles_download_passes_requested_episode_to_zip_extraction(self):
        http_post = FakeHttpPostSequence(
            [
                {"token": "jwt-token", "base_url": "api.opensubtitles.com"},
                {"link": "https://download.opensubtitles.test/show-season.zip", "file_name": "Show.Season.6.en.zip"},
            ]
        )
        http_download = FakeHttpDownload(
            zip_payload_files(
                [
                    ("Show.S06E01.en.srt", "episode one\n"),
                    ("Show.S06E15.en.srt", "episode fifteen\n"),
                ]
            )
        )
        provider = OpenSubtitlesProvider(
            OpenSubtitlesConfig.from_values(
                api_key="os-key",
                username="opensub-user",
                password="opensub-password",
            ),
            http_post=http_post,
            http_download=http_download,
        )
        candidate = SubtitleCandidate(
            provider="opensubtitles",
            provider_id="os-episode",
            language="en",
            file_id="2712566",
            file_name="Show.Season.6.en.zip",
            download={"method": "opensubtitles-download", "file_id": "2712566", "requires_token": True},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp), target_season=6, target_episode=15)

            self.assertEqual(result.path.name, "Show.S06E15.en.srt")
            self.assertEqual(result.path.read_text(encoding="utf-8"), "episode fifteen\n")
            self.assertTrue(result.extracted_from_archive)

    def test_subdl_searches_by_imdb_and_parses_candidates(self):
        http = FakeHttpGet(
            {
                "status": True,
                "subtitles": [
                    {
                        "release_name": "Inception.2010.1080p.BluRay",
                        "name": "Inception.2010.en.srt",
                        "url": "/subtitle/3197651-3213944.zip",
                        "language": "EN",
                        "hi": False,
                        "format": "srt",
                        "season": None,
                        "episode": None,
                    }
                ],
            }
        )
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_get=http)

        results = provider.search(
            SubtitleSearchRequest(
                media_type="movie",
                imdb_id="tt1375666",
                languages=("en",),
                limit=10,
            )
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(http.calls[0]["url"], "https://api.subdl.com/api/v1/subtitles")
        self.assertEqual(http.calls[0]["params"]["api_key"], "subdl-key")
        self.assertEqual(http.calls[0]["params"]["imdb_id"], "tt1375666")
        self.assertEqual(http.calls[0]["params"]["languages"], "EN")
        self.assertEqual(http.calls[0]["params"]["type"], "movie")
        self.assertEqual(http.calls[0]["params"]["unpack"], "1")
        self.assertEqual(http.calls[0]["params"]["subs_per_page"], "10")
        self.assertEqual(results[0].provider, "subdl")
        self.assertEqual(results[0].provider_id, "/subtitle/3197651-3213944.zip")
        self.assertEqual(results[0].language, "en")
        self.assertEqual(results[0].download["url"], "https://dl.subdl.com/subtitle/3197651-3213944.zip")

    def test_subdl_search_uses_safe_title_before_unsafe_file_name(self):
        http = FakeHttpGet({"status": True, "subtitles": []})
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_get=http)

        provider.search(
            SubtitleSearchRequest(
                media_type="movie",
                title="The Ballad of Buster Scruggs",
                file_name="The.Ballad.Of.Buster.Scruggs.2018.1080p.WEBRip.x264-[YTS.AM].mp4",
                imdb_id="tt6412452",
                languages=("en",),
            )
        )

        self.assertEqual(http.calls[0]["params"]["film_name"], "The Ballad of Buster Scruggs")

    def test_subdl_searches_tv_episode_with_season_episode(self):
        http = FakeHttpGet({"status": True, "subtitles": []})
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_get=http)

        provider.search(
            SubtitleSearchRequest(
                media_type="episode",
                title="Example Show",
                season=1,
                episode=3,
                languages=("fr",),
            )
        )

        self.assertEqual(http.calls[0]["params"]["film_name"], "Example Show")
        self.assertEqual(http.calls[0]["params"]["season_number"], "1")
        self.assertEqual(http.calls[0]["params"]["episode_number"], "3")
        self.assertEqual(http.calls[0]["params"]["languages"], "FR")
        self.assertEqual(http.calls[0]["params"]["type"], "tv")

    def test_subdl_download_saves_direct_subtitle_file(self):
        http_download = FakeHttpDownload(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_download=http_download)
        candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/3197651-3213944.srt",
            language="en",
            file_name="Inception.2010.en.srt",
            download={"method": "direct-url", "url": "https://dl.subdl.com/subtitle/3197651-3213944.srt"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp))

            self.assertEqual(result.provider, "subdl")
            self.assertEqual(result.path.name, "Inception.2010.en.srt")
            self.assertEqual(result.path.read_text(encoding="utf-8"), "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
            self.assertFalse(result.extracted_from_archive)
            self.assertEqual(http_download.calls[0]["url"], "https://dl.subdl.com/subtitle/3197651-3213944.srt")
            self.assertEqual(http_download.calls[0]["headers"]["User-Agent"], "MediaSubtitleTranslator v0.1.0")

    def test_subdl_download_keeps_microdvd_sub_extension(self):
        http_download = FakeHttpDownload(b"{1}{1}23.976\n{24}{48}Hello\n")
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_download=http_download)
        candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/movie.sub",
            language="en",
            file_name="Movie.en.sub",
            download={"method": "direct-url", "url": "https://dl.subdl.com/subtitle/movie.sub"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp))

            self.assertEqual(result.path.name, "Movie.en.sub")
            self.assertEqual(result.path.read_text(encoding="utf-8"), "{1}{1}23.976\n{24}{48}Hello\n")

    def test_subdl_download_extracts_first_supported_subtitle_from_zip(self):
        payload = zip_payload("nested/Inception.2010.en.srt", "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
        http_download = FakeHttpDownload(payload)
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_download=http_download)
        candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/3197651-3213944.zip",
            language="en",
            file_name="Inception.2010.en.zip",
            download={"method": "direct-url", "url": "https://dl.subdl.com/subtitle/3197651-3213944.zip"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp))

            self.assertEqual(result.provider, "subdl")
            self.assertEqual(result.path.name, "Inception.2010.en.srt")
            self.assertEqual(result.path.read_text(encoding="utf-8"), "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
            self.assertTrue(result.extracted_from_archive)
            self.assertEqual(result.archive_path.name, "Inception.2010.en.zip")

    def test_subdl_download_extracts_microdvd_sub_from_zip_when_no_srt_exists(self):
        payload = zip_payload("nested/Inception.2010.en.sub", "{1}{1}23.976\n{24}{48}Hello\n")
        http_download = FakeHttpDownload(payload)
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_download=http_download)
        candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/3197651-3213944.zip",
            language="en",
            file_name="Inception.2010.en.zip",
            download={"method": "direct-url", "url": "https://dl.subdl.com/subtitle/3197651-3213944.zip"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp))

            self.assertEqual(result.path.name, "Inception.2010.en.sub")
            self.assertEqual(result.path.read_text(encoding="utf-8"), "{1}{1}23.976\n{24}{48}Hello\n")
            self.assertTrue(result.extracted_from_archive)

    def test_subdl_download_extracts_zip_payload_when_suggested_name_is_srt(self):
        payload = zip_payload("nested/Inception.2010.en.srt", "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")
        http_download = FakeHttpDownload(payload)
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_download=http_download)
        candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/3197651-3213944.srt",
            language="en",
            file_name="Inception.2010.en.srt",
            download={"method": "direct-url", "url": "https://dl.subdl.com/subtitle/3197651-3213944.srt"},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp))

            self.assertEqual(result.path.name, "Inception.2010.en.srt")
            self.assertEqual(result.archive_path.name, "Inception.2010.en.zip")
            self.assertTrue(result.extracted_from_archive)

    def test_subdl_download_extracts_matching_episode_from_zip(self):
        payload = zip_payload_files(
            [
                ("Show.S06E01.en.srt", "episode one\n"),
                ("Show.S06E15.en.srt", "episode fifteen\n"),
            ]
        )
        http_download = FakeHttpDownload(payload)
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_download=http_download)
        candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/show-season-pack.zip",
            language="en",
            file_name="Show.S06E15.en.zip",
            download={"method": "direct-url", "url": "https://dl.subdl.com/subtitle/show-season-pack.zip"},
            metadata={"season": 6, "episode": 15},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp))

            self.assertEqual(result.path.name, "Show.S06E15.en.srt")
            self.assertEqual(result.path.read_text(encoding="utf-8"), "episode fifteen\n")
            self.assertTrue(result.extracted_from_archive)

    def test_subdl_download_extracts_requested_episode_from_season_pack_zip(self):
        payload = zip_payload_files(
            [
                ("Show.S06E01.en.srt", "episode one\n"),
                ("Show.S06E15.en.srt", "episode fifteen\n"),
            ]
        )
        http_download = FakeHttpDownload(payload)
        provider = SubDLProvider(SubDLConfig(api_key="subdl-key"), http_download=http_download)
        candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/show-season-pack.zip",
            language="en",
            file_name="Show.Season.6.Complete.en.zip",
            download={"method": "direct-url", "url": "https://dl.subdl.com/subtitle/show-season-pack.zip"},
            metadata={"season": 6, "episode": None},
        )

        with tempfile.TemporaryDirectory() as tmp:
            result = provider.download(candidate, Path(tmp), target_season=6, target_episode=15)

            self.assertEqual(result.path.name, "Show.S06E15.en.srt")
            self.assertEqual(result.path.read_text(encoding="utf-8"), "episode fifteen\n")
            self.assertTrue(result.extracted_from_archive)

    def test_missing_provider_configuration_has_clear_errors(self):
        with self.assertRaisesRegex(SubtitleProviderConfigurationError, "OPENSUBTITLES_API_KEY is required"):
            OpenSubtitlesConfig.from_values(api_key=None)
        with self.assertRaisesRegex(SubtitleProviderConfigurationError, "SUBDL_API_KEY is required"):
            SubDLConfig.from_values(api_key=None)

    def test_subtitle_search_parser_accepts_provider_and_language_options(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "subtitle-search",
                "--provider",
                "subdl",
                "--imdb",
                "tt1375666",
                "--media-type",
                "movie",
                "--language",
                "en",
                "--limit",
                "7",
            ]
        )

        self.assertEqual(args.provider, "subdl")
        self.assertEqual(args.imdb, "tt1375666")
        self.assertEqual(args.media_type, "movie")
        self.assertEqual(args.languages, ["en"])
        self.assertEqual(args.limit, 7)

    def test_subtitle_search_defaults_to_all_configured_providers(self):
        parser = build_parser()

        args = parser.parse_args(["subtitle-search", "--imdb", "tt1375666"])

        self.assertEqual(args.provider, "all")
        self.assertEqual(provider_names_for_search(args.provider), ["subdl", "opensubtitles"])

    def test_subtitle_search_summary_queries_both_providers_and_orders_subdl_first(self):
        parser = build_parser()
        args = parser.parse_args(["subtitle-search", "--imdb", "tt1375666", "--language", "en"])
        subdl_candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/3197651-3213944.srt",
            language="en",
            file_name="Inception.2010.en.srt",
            download={"url": "https://dl.subdl.com/subtitle/3197651-3213944.srt"},
        )
        opensubtitles_candidate = SubtitleCandidate(
            provider="opensubtitles",
            provider_id="os-1",
            language="en",
            file_id="2712566",
            file_name="Inception.2010.en.srt",
        )
        providers = {
            "subdl": FakeSearchDownloadProvider("subdl", [subdl_candidate]),
            "opensubtitles": FakeSearchDownloadProvider("opensubtitles", [opensubtitles_candidate]),
        }
        original_builder = cli_module.build_subtitle_provider
        cli_module.build_subtitle_provider = lambda provider_name, _args: providers[provider_name]
        try:
            summary = subtitle_search_summary(args)
        finally:
            cli_module.build_subtitle_provider = original_builder

        self.assertEqual([provider["name"] for provider in summary["providers"]], ["subdl", "opensubtitles"])
        self.assertEqual([result["provider"] for result in summary["results"]], ["subdl", "opensubtitles"])
        self.assertEqual(summary["download_provider_priority"], ["subdl", "opensubtitles"])

    def test_subtitle_search_parser_accepts_opensubtitles_login_options(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "subtitle-search",
                "--provider",
                "opensubtitles",
                "--imdb",
                "tt1375666",
                "--opensubtitles-api-key",
                "os-key",
                "--opensubtitles-username",
                "opensub-user",
                "--opensubtitles-password",
                "opensub-password",
            ]
        )

        self.assertEqual(args.opensubtitles_api_key, "os-key")
        self.assertEqual(args.opensubtitles_username, "opensub-user")
        self.assertEqual(args.opensubtitles_password, "opensub-password")

    def test_subtitle_download_parser_accepts_provider_identifiers(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "subtitle-download",
                "--provider",
                "opensubtitles",
                "--file-id",
                "2712566",
                "--file-name",
                "Inception.2010.en.srt",
                "--output-dir",
                ".runtime/downloads",
            ]
        )

        self.assertEqual(args.provider, "opensubtitles")
        self.assertEqual(args.file_id, "2712566")
        self.assertEqual(args.file_name, "Inception.2010.en.srt")
        self.assertEqual(args.output_dir, Path(".runtime/downloads"))

    def test_download_priority_ranks_subdl_before_opensubtitles(self):
        candidates = [
            SubtitleCandidate(
                provider="opensubtitles",
                provider_id="os-1",
                language="en",
                file_id="2712566",
                file_name="Inception.2010.en.srt",
            ),
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/3197651-3213944.srt",
                language="en",
                file_name="Inception.2010.en.srt",
                download={"url": "https://dl.subdl.com/subtitle/3197651-3213944.srt"},
            ),
        ]

        ranked = rank_candidates_for_download(candidates)

        self.assertEqual([candidate.provider for candidate in ranked], ["subdl", "opensubtitles"])

    def test_release_match_tie_prefers_srt_over_microdvd_sub(self):
        candidates = [
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/movie.sub",
                language="en",
                release_name="Movie.2018.1080p.BluRay.x264-GRP",
                file_name="Movie.2018.1080p.BluRay.x264-GRP.sub",
                subtitle_format="sub",
                download={"url": "https://dl.subdl.com/subtitle/movie.sub"},
            ),
            SubtitleCandidate(
                provider="opensubtitles",
                provider_id="os-srt",
                language="en",
                release_name="Movie.2018.1080p.BluRay.x264-GRP",
                file_name="Movie.2018.1080p.BluRay.x264-GRP.srt",
                subtitle_format="srt",
                file_id="2712566",
            ),
        ]

        ranked = rank_candidates_for_download(
            candidates,
            media_release_name="Movie.2018.1080p.BluRay.x264-GRP.mkv",
        )

        self.assertEqual([candidate.file_name for candidate in ranked], [
            "Movie.2018.1080p.BluRay.x264-GRP.srt",
            "Movie.2018.1080p.BluRay.x264-GRP.sub",
        ])

    def test_release_match_ranking_beats_provider_priority_when_media_file_is_known(self):
        candidates = [
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/bluray.zip",
                language="en",
                release_name="Movie.2018.1080p.BluRay.x264-GRP",
                file_name="Movie.2018.1080p.BluRay.x264-GRP.srt",
                download={"url": "https://dl.subdl.com/subtitle/bluray.zip"},
            ),
            SubtitleCandidate(
                provider="opensubtitles",
                provider_id="os-web",
                language="en",
                release_name="Movie.2018.720p.WEB-DL.x264-OTHER",
                file_id="2712566",
            ),
        ]

        ranked = rank_candidates_for_download(
            candidates,
            media_release_name="Movie.2018.1080p.WEBRip.x264-GRP.mkv",
        )

        self.assertEqual([candidate.provider for candidate in ranked], ["opensubtitles", "subdl"])

    def test_release_match_ranking_prefers_bluray_candidate_for_brrip_media(self):
        candidates = [
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/webdl.zip",
                language="en",
                release_name="Movie.2018.1080p.WEB-DL.x264-OTHER",
                file_name="Movie.2018.1080p.WEB-DL.x264-OTHER.srt",
                download={"url": "https://dl.subdl.com/subtitle/webdl.zip"},
            ),
            SubtitleCandidate(
                provider="opensubtitles",
                provider_id="os-bluray",
                language="en",
                release_name="Movie.2018.720p.BluRay.x264-GRP",
                file_id="2712566",
            ),
        ]

        ranked = rank_candidates_for_download(
            candidates,
            media_release_name="Movie.2018.1080p.BRRip.x264-GRP.mkv",
        )

        self.assertEqual([candidate.provider for candidate in ranked], ["opensubtitles", "subdl"])

    def test_download_first_provider_candidate_tries_subdl_before_opensubtitles(self):
        candidates = [
            SubtitleCandidate(
                provider="opensubtitles",
                provider_id="os-1",
                language="en",
                file_id="2712566",
                file_name="Inception.2010.en.srt",
            ),
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/3197651-3213944.srt",
                language="en",
                file_name="Inception.2010.en.srt",
                download={"url": "https://dl.subdl.com/subtitle/3197651-3213944.srt"},
            ),
        ]
        subdl = FakeDownloadProvider("subdl")
        opensubtitles = FakeDownloadProvider("opensubtitles")

        with tempfile.TemporaryDirectory() as tmp:
            selection = download_first_provider_candidate(
                candidates,
                {"opensubtitles": opensubtitles, "subdl": subdl},
                Path(tmp),
            )

        self.assertEqual(selection.candidate.provider, "subdl")
        self.assertEqual(len(subdl.calls), 1)
        self.assertEqual(len(opensubtitles.calls), 0)
        self.assertEqual(selection.attempts[0]["provider"], "subdl")
        self.assertEqual(selection.attempts[0]["status"], "ok")

    def test_download_first_provider_candidate_skips_low_confidence_release_match(self):
        candidates = [
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/bluray.zip",
                language="en",
                release_name="Movie.2018.1080p.BluRay.x264-GRP",
                file_name="Movie.2018.1080p.BluRay.x264-GRP.srt",
                download={"url": "https://dl.subdl.com/subtitle/bluray.zip"},
            ),
            SubtitleCandidate(
                provider="opensubtitles",
                provider_id="os-web",
                language="en",
                release_name="Movie.2018.720p.WEB-DL.x264-OTHER",
                file_id="2712566",
            ),
        ]
        subdl = FakeDownloadProvider("subdl")
        opensubtitles = FakeDownloadProvider("opensubtitles")

        with tempfile.TemporaryDirectory() as tmp:
            selection = download_first_provider_candidate(
                candidates,
                {"opensubtitles": opensubtitles, "subdl": subdl},
                Path(tmp),
                media_release_name="Movie.2018.1080p.WEBRip.x264-GRP.mkv",
            )

        self.assertEqual(selection.candidate.provider, "opensubtitles")
        self.assertEqual(len(subdl.calls), 0)
        self.assertEqual(len(opensubtitles.calls), 1)
        self.assertEqual(selection.match["confidence"], "high")
        self.assertEqual(selection.attempts[0]["provider"], "opensubtitles")
        self.assertEqual(selection.attempts[0]["status"], "ok")

    def test_download_first_provider_candidate_rejects_mismatched_episode(self):
        wrong_episode = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/show-s06e01.zip",
            language="en",
            release_name="Show.S06E01.1080p.WEBRip.x264-GRP",
            file_name="Show.S06E01.1080p.WEBRip.x264-GRP.srt",
            download={"url": "https://dl.subdl.com/subtitle/show-s06e01.zip"},
            metadata={"season": 6, "episode": 1},
        )
        subdl = FakeDownloadProvider("subdl")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SubtitleProviderApiError, "no subtitle candidates could be downloaded"):
                download_first_provider_candidate(
                    [wrong_episode],
                    {"subdl": subdl},
                    Path(tmp),
                    media_release_name="Show.S06E15.1080p.WEBRip.x264-GRP.mkv",
                )

        self.assertEqual(len(subdl.calls), 0)

    def test_download_first_provider_candidate_passes_requested_episode_to_download(self):
        season_pack = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/show-season-pack.zip",
            language="en",
            release_name="Show.Season.6.Complete.WEBRip.x264-GRP",
            file_name="Show.Season.6.Complete.en.zip",
            download={"url": "https://dl.subdl.com/subtitle/show-season-pack.zip"},
            metadata={"season": 6, "episode": None},
        )
        subdl = FakeDownloadProvider("subdl")

        with tempfile.TemporaryDirectory() as tmp:
            selection = download_first_provider_candidate(
                [season_pack],
                {"subdl": subdl},
                Path(tmp),
                media_release_name="Show.S06E15.1080p.WEBRip.x264-GRP.mkv",
            )

        self.assertEqual(selection.candidate.provider, "subdl")
        self.assertEqual(subdl.calls[0]["target_season"], 6)
        self.assertEqual(subdl.calls[0]["target_episode"], 15)

    def test_download_first_provider_candidate_rejects_all_low_confidence_matches(self):
        candidates = [
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/webrip-other.zip",
                language="en",
                release_name="Different.720p.WEB-DL.x265-OTHER",
                file_name="Different.720p.WEB-DL.x265-OTHER.srt",
                download={"url": "https://dl.subdl.com/subtitle/webrip-other.zip"},
            ),
        ]
        subdl = FakeDownloadProvider("subdl")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(LowConfidenceSubtitleCandidatesError, "low-confidence subtitle candidates require confirmation"):
                download_first_provider_candidate(
                    candidates,
                    {"subdl": subdl},
                    Path(tmp),
                    media_release_name="Movie.2018.1080p.WEBRip.x264-GRP.mkv",
                )

        self.assertEqual(len(subdl.calls), 0)

    def test_download_first_provider_candidate_can_use_low_confidence_after_confirmation(self):
        candidates = [
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/webrip-other.zip",
                language="en",
                release_name="Different.720p.WEB-DL.x265-OTHER",
                file_name="Different.720p.WEB-DL.x265-OTHER.srt",
                download={"url": "https://dl.subdl.com/subtitle/webrip-other.zip"},
            ),
        ]
        subdl = FakeDownloadProvider("subdl")

        with tempfile.TemporaryDirectory() as tmp:
            selection = download_first_provider_candidate(
                candidates,
                {"subdl": subdl},
                Path(tmp),
                media_release_name="Movie.2018.1080p.WEBRip.x264-GRP.mkv",
                allow_low_confidence=True,
            )

        self.assertEqual(selection.candidate.provider, "subdl")
        self.assertEqual(selection.match["confidence"], "low")
        self.assertEqual(len(subdl.calls), 1)

    def test_download_first_provider_candidate_rejects_web_subtitle_for_brrip_media_even_when_confirmed(self):
        candidates = [
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/webdl.zip",
                language="en",
                release_name="Movie.2018.1080p.WEB-DL.x264-OTHER",
                file_name="Movie.2018.1080p.WEB-DL.x264-OTHER.srt",
                download={"url": "https://dl.subdl.com/subtitle/webdl.zip"},
            ),
        ]
        subdl = FakeDownloadProvider("subdl")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SubtitleProviderApiError, "no subtitle candidates could be downloaded"):
                download_first_provider_candidate(
                    candidates,
                    {"subdl": subdl},
                    Path(tmp),
                    media_release_name="Movie.2018.1080p.BRRip.x264-GRP.mkv",
                    allow_low_confidence=True,
                )

        self.assertEqual(len(subdl.calls), 0)

    def test_download_first_provider_candidate_falls_back_to_opensubtitles(self):
        candidates = [
            SubtitleCandidate(
                provider="subdl",
                provider_id="/subtitle/3197651-3213944.srt",
                language="en",
                file_name="Inception.2010.en.srt",
                download={"url": "https://dl.subdl.com/subtitle/3197651-3213944.srt"},
            ),
            SubtitleCandidate(
                provider="opensubtitles",
                provider_id="os-1",
                language="en",
                file_id="2712566",
                file_name="Inception.2010.en.srt",
            ),
        ]
        subdl = FakeDownloadProvider("subdl", error=SubtitleProviderApiError("SubDL quota exhausted"))
        opensubtitles = FakeDownloadProvider("opensubtitles")

        with tempfile.TemporaryDirectory() as tmp:
            selection = download_first_provider_candidate(
                candidates,
                {"opensubtitles": opensubtitles, "subdl": subdl},
                Path(tmp),
            )

        self.assertEqual(selection.candidate.provider, "opensubtitles")
        self.assertEqual(len(subdl.calls), 1)
        self.assertEqual(len(opensubtitles.calls), 1)
        self.assertEqual([attempt["status"] for attempt in selection.attempts], ["error", "ok"])

    def test_subtitle_fetch_parser_accepts_search_and_priority_options(self):
        parser = build_parser()

        args = parser.parse_args(
            [
                "subtitle-fetch",
                "--imdb",
                "tt1375666",
                "--language",
                "en",
                "--output-dir",
                ".runtime/provider-fetch",
            ]
        )

        self.assertEqual(args.provider, "all")
        self.assertEqual(args.download_provider_priority, "subdl,opensubtitles")
        self.assertEqual(args.output_dir, Path(".runtime/provider-fetch"))
        self.assertFalse(args.allow_low_confidence_subtitle)

    def test_subtitle_fetch_summary_downloads_subdl_candidate_first(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "subtitle-fetch",
                "--imdb",
                "tt1375666",
                "--language",
                "en",
                "--output-dir",
                ".runtime/provider-fetch",
            ]
        )
        subdl_candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/3197651-3213944.srt",
            language="en",
            file_name="Inception.2010.en.srt",
            download={"url": "https://dl.subdl.com/subtitle/3197651-3213944.srt"},
        )
        opensubtitles_candidate = SubtitleCandidate(
            provider="opensubtitles",
            provider_id="os-1",
            language="en",
            file_id="2712566",
            file_name="Inception.2010.en.srt",
        )
        providers = {
            "subdl": FakeSearchDownloadProvider("subdl", [subdl_candidate]),
            "opensubtitles": FakeSearchDownloadProvider("opensubtitles", [opensubtitles_candidate]),
        }
        original_builder = cli_module.build_subtitle_provider
        cli_module.build_subtitle_provider = lambda provider_name, _args: providers[provider_name]
        try:
            summary = subtitle_fetch_summary(args)
        finally:
            cli_module.build_subtitle_provider = original_builder

        self.assertEqual(summary["selected"]["candidate"]["provider"], "subdl")
        self.assertEqual(len(providers["subdl"].calls), 1)
        self.assertEqual(len(providers["opensubtitles"].calls), 0)

    def test_subtitle_fetch_summary_uses_release_matching_when_file_name_is_known(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "subtitle-fetch",
                "--imdb",
                "tt1234567",
                "--file-name",
                "Movie.2018.1080p.WEBRip.x264-GRP.mkv",
                "--language",
                "en",
                "--output-dir",
                ".runtime/provider-fetch",
            ]
        )
        subdl_candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/webrip-other.zip",
            language="en",
            release_name="Different.720p.WEB-DL.x265-OTHER",
            file_name="Different.720p.WEB-DL.x265-OTHER.srt",
            download={"url": "https://dl.subdl.com/subtitle/webrip-other.zip"},
        )
        opensubtitles_candidate = SubtitleCandidate(
            provider="opensubtitles",
            provider_id="os-web",
            language="en",
            release_name="Movie.2018.720p.WEB-DL.x264-OTHER",
            file_id="2712566",
        )
        providers = {
            "subdl": FakeSearchDownloadProvider("subdl", [subdl_candidate]),
            "opensubtitles": FakeSearchDownloadProvider("opensubtitles", [opensubtitles_candidate]),
        }
        original_builder = cli_module.build_subtitle_provider
        cli_module.build_subtitle_provider = lambda provider_name, _args: providers[provider_name]
        try:
            summary = subtitle_fetch_summary(args)
        finally:
            cli_module.build_subtitle_provider = original_builder

        self.assertEqual(summary["selected"]["candidate"]["provider"], "opensubtitles")
        self.assertEqual(summary["selected"]["match"]["confidence"], "high")
        self.assertEqual(len(providers["subdl"].calls), 0)
        self.assertEqual(len(providers["opensubtitles"].calls), 1)

    def test_subtitle_fetch_overfetches_before_release_matching_when_file_name_is_known(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "subtitle-fetch",
                "--provider",
                "opensubtitles",
                "--imdb",
                "tt1234567",
                "--file-name",
                "Movie.2018.1080p.BluRay.x264-YIFY.mkv",
                "--language",
                "en",
                "--limit",
                "10",
                "--output-dir",
                ".runtime/provider-fetch",
            ]
        )
        weaker_candidates = [
            SubtitleCandidate(
                provider="opensubtitles",
                provider_id="os-720-%02d" % index,
                language="en",
                release_name="Movie.2018.720p.BluRay.x264-YIFY",
                file_name="Movie.2018.720p.BluRay.x264-YIFY.%02d.srt" % index,
                file_id=str(1000 + index),
            )
            for index in range(10)
        ]
        exact_candidate = SubtitleCandidate(
            provider="opensubtitles",
            provider_id="os-exact",
            language="en",
            release_name="Movie.2018.1080p.BluRay.x264-YIFY",
            file_name="Movie.2018.1080p.BluRay.x264-YIFY.srt",
            file_id="2712566",
        )
        provider = LimitAwareSearchDownloadProvider("opensubtitles", weaker_candidates + [exact_candidate])
        original_builder = cli_module.build_subtitle_provider
        cli_module.build_subtitle_provider = lambda provider_name, _args: provider
        try:
            summary = subtitle_fetch_summary(args)
        finally:
            cli_module.build_subtitle_provider = original_builder

        self.assertGreater(provider.search_calls[0].limit, args.limit)
        self.assertEqual(summary["selected"]["candidate"]["provider_id"], "os-exact")
        self.assertIn("exact_release_match", summary["selected"]["match"]["reasons"])
        self.assertEqual(len(provider.calls), 1)

    def test_subtitle_fetch_summary_returns_low_confidence_proposal_without_downloading(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "subtitle-fetch",
                "--provider",
                "subdl",
                "--imdb",
                "tt1234567",
                "--file-name",
                "Movie.2018.1080p.WEBRip.x264-GRP.mkv",
                "--language",
                "en",
                "--output-dir",
                ".runtime/provider-fetch",
            ]
        )
        low_candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/webrip-other.zip",
            language="en",
            release_name="Different.720p.WEB-DL.x265-OTHER",
            file_name="Different.720p.WEB-DL.x265-OTHER.srt",
            download={"url": "https://dl.subdl.com/subtitle/webrip-other.zip"},
        )
        providers = {"subdl": FakeSearchDownloadProvider("subdl", [low_candidate])}
        original_builder = cli_module.build_subtitle_provider
        cli_module.build_subtitle_provider = lambda provider_name, _args: providers[provider_name]
        try:
            summary = subtitle_fetch_summary(args)
        finally:
            cli_module.build_subtitle_provider = original_builder

        self.assertIsNone(summary["selected"])
        self.assertEqual(summary["proposal"]["action"], "confirm_low_confidence_subtitle")
        self.assertEqual(summary["proposal"]["message_key"], "low_confidence_subtitle_confirmation")
        self.assertIn("may not match the video timeline", summary["proposal"]["message"])
        self.assertNotRegex(summary["proposal"]["message"], r"[\u4e00-\u9fff]")
        self.assertEqual(summary["proposal"]["candidates"][0]["match"]["confidence"], "low")
        self.assertEqual(len(providers["subdl"].calls), 0)

    def test_subtitle_fetch_summary_downloads_low_confidence_when_confirmed(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "subtitle-fetch",
                "--provider",
                "subdl",
                "--imdb",
                "tt1234567",
                "--file-name",
                "Movie.2018.1080p.WEBRip.x264-GRP.mkv",
                "--language",
                "en",
                "--output-dir",
                ".runtime/provider-fetch",
                "--allow-low-confidence-subtitle",
            ]
        )
        low_candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/webrip-other.zip",
            language="en",
            release_name="Different.720p.WEB-DL.x265-OTHER",
            file_name="Different.720p.WEB-DL.x265-OTHER.srt",
            download={"url": "https://dl.subdl.com/subtitle/webrip-other.zip"},
        )
        providers = {"subdl": FakeSearchDownloadProvider("subdl", [low_candidate])}
        original_builder = cli_module.build_subtitle_provider
        cli_module.build_subtitle_provider = lambda provider_name, _args: providers[provider_name]
        try:
            summary = subtitle_fetch_summary(args)
        finally:
            cli_module.build_subtitle_provider = original_builder

        self.assertEqual(summary["selected"]["candidate"]["provider"], "subdl")
        self.assertEqual(summary["selected"]["match"]["confidence"], "low")
        self.assertEqual(len(providers["subdl"].calls), 1)

    def test_translate_plex_provider_fetcher_uses_resolved_media_file_for_release_matching(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "translate-plex",
                "--rating-key",
                "131",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
                "--allow-low-confidence-subtitle",
            ]
        )
        resolved = PlexResolvedMedia(
            rating_key="131",
            title="Movie",
            media_type="movie",
            plex_file="/server/media/Movies/Movie.2018.1080p.WEBRip.x264-GRP.mkv",
            local_file="/mnt/media/Movies/Movie.2018.1080p.WEBRip.x264-GRP.mkv",
            path_mapping_applied=True,
            imdb="tt1234567",
        )
        subdl_candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/webrip-other.zip",
            language="en",
            release_name="Different.720p.WEB-DL.x265-OTHER",
            file_name="Different.720p.WEB-DL.x265-OTHER.srt",
            download={"url": "https://dl.subdl.com/subtitle/webrip-other.zip"},
        )
        opensubtitles_candidate = SubtitleCandidate(
            provider="opensubtitles",
            provider_id="os-web",
            language="en",
            release_name="Movie.2018.720p.WEB-DL.x264-OTHER",
            file_id="2712566",
        )
        providers = {
            "subdl": FakeSearchDownloadProvider("subdl", [subdl_candidate]),
            "opensubtitles": FakeSearchDownloadProvider("opensubtitles", [opensubtitles_candidate]),
        }
        original_builder = cli_module.build_subtitle_provider
        cli_module.build_subtitle_provider = lambda provider_name, _args: providers[provider_name]
        try:
            fetcher = build_plex_online_subtitle_fetcher(args)
            with tempfile.TemporaryDirectory() as tmp:
                selection = fetcher(resolved, "en", Path(tmp))
        finally:
            cli_module.build_subtitle_provider = original_builder

        self.assertEqual(selection.candidate.provider, "opensubtitles")
        self.assertEqual(selection.match["confidence"], "high")
        self.assertEqual(len(providers["subdl"].calls), 0)
        self.assertEqual(len(providers["opensubtitles"].calls), 1)

    def test_translate_plex_provider_fetcher_downloads_low_confidence_when_confirmed(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "translate-plex",
                "--rating-key",
                "131",
                "--plex-base-url",
                "http://127.0.0.1:32400",
                "--plex-token",
                "token",
                "--subtitle-provider",
                "subdl",
                "--allow-low-confidence-subtitle",
            ]
        )
        resolved = PlexResolvedMedia(
            rating_key="131",
            title="Movie",
            media_type="movie",
            plex_file="/server/media/Movies/Movie.2018.1080p.WEBRip.x264-GRP.mkv",
            local_file="/mnt/media/Movies/Movie.2018.1080p.WEBRip.x264-GRP.mkv",
            path_mapping_applied=True,
            imdb="tt1234567",
        )
        subdl_candidate = SubtitleCandidate(
            provider="subdl",
            provider_id="/subtitle/webrip-other.zip",
            language="en",
            release_name="Different.720p.WEB-DL.x265-OTHER",
            file_name="Different.720p.WEB-DL.x265-OTHER.srt",
            download={"url": "https://dl.subdl.com/subtitle/webrip-other.zip"},
        )
        providers = {"subdl": FakeSearchDownloadProvider("subdl", [subdl_candidate])}
        original_builder = cli_module.build_subtitle_provider
        cli_module.build_subtitle_provider = lambda provider_name, _args: providers[provider_name]
        try:
            fetcher = build_plex_online_subtitle_fetcher(args)
            with tempfile.TemporaryDirectory() as tmp:
                selection = fetcher(resolved, "en", Path(tmp))
        finally:
            cli_module.build_subtitle_provider = original_builder

        self.assertEqual(selection.candidate.provider, "subdl")
        self.assertEqual(selection.match["confidence"], "low")
        self.assertEqual(len(providers["subdl"].calls), 1)


if __name__ == "__main__":
    unittest.main()
