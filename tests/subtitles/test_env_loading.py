import os
import tempfile
import unittest
from pathlib import Path

from babelarr.cli import build_parser


class EnvLoadingTests(unittest.TestCase):
    def test_build_parser_loads_project_dotenv_without_overriding_environment(self):
        old_cwd = Path.cwd()
        old_subdl = os.environ.pop("SUBDL_API_KEY", None)
        old_provider = os.environ.pop("MST_SUBTITLE_PROVIDER", None)
        old_opensubtitles_username = os.environ.pop("OPENSUBTITLES_USERNAME", None)
        os.environ["SUBDL_API_KEY"] = "from-real-env"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / ".env").write_text(
                    "\n".join(
                        [
                            "MST_SUBTITLE_PROVIDER=subdl",
                            "SUBDL_API_KEY=from-dotenv",
                            'OPENSUBTITLES_USERNAME="opensub-user"',
                        ]
                    ),
                    encoding="utf-8",
                )
                os.chdir(root)

                parser = build_parser()
                args = parser.parse_args(["subtitle-search", "--imdb", "tt1375666"])

                self.assertEqual(args.provider, "subdl")
                self.assertEqual(args.subdl_api_key, "from-real-env")
                self.assertEqual(args.opensubtitles_username, "opensub-user")
        finally:
            os.chdir(old_cwd)
            os.environ.pop("SUBDL_API_KEY", None)
            os.environ.pop("MST_SUBTITLE_PROVIDER", None)
            if old_subdl is not None:
                os.environ["SUBDL_API_KEY"] = old_subdl
            if old_provider is not None:
                os.environ["MST_SUBTITLE_PROVIDER"] = old_provider
            if old_opensubtitles_username is not None:
                os.environ["OPENSUBTITLES_USERNAME"] = old_opensubtitles_username

    def test_babelarr_env_names_are_preferred_over_legacy_mst_names(self):
        old_provider = os.environ.pop("MST_SUBTITLE_PROVIDER", None)
        old_babelarr_provider = os.environ.pop("BABELARR_SUBTITLE_PROVIDER", None)
        old_priority = os.environ.pop("MST_SUBTITLE_DOWNLOAD_PRIORITY", None)
        old_babelarr_priority = os.environ.pop("BABELARR_SUBTITLE_DOWNLOAD_PRIORITY", None)
        try:
            os.environ["MST_SUBTITLE_PROVIDER"] = "opensubtitles"
            os.environ["BABELARR_SUBTITLE_PROVIDER"] = "subdl"
            os.environ["MST_SUBTITLE_DOWNLOAD_PRIORITY"] = "opensubtitles,subdl"
            os.environ["BABELARR_SUBTITLE_DOWNLOAD_PRIORITY"] = "subdl,opensubtitles"

            parser = build_parser()
            args = parser.parse_args(["subtitle-search", "--imdb", "tt1375666"])

            self.assertEqual(args.provider, "subdl")
            self.assertEqual(args.download_provider_priority, "subdl,opensubtitles")
        finally:
            os.environ.pop("MST_SUBTITLE_PROVIDER", None)
            os.environ.pop("BABELARR_SUBTITLE_PROVIDER", None)
            os.environ.pop("MST_SUBTITLE_DOWNLOAD_PRIORITY", None)
            os.environ.pop("BABELARR_SUBTITLE_DOWNLOAD_PRIORITY", None)
            if old_provider is not None:
                os.environ["MST_SUBTITLE_PROVIDER"] = old_provider
            if old_babelarr_provider is not None:
                os.environ["BABELARR_SUBTITLE_PROVIDER"] = old_babelarr_provider
            if old_priority is not None:
                os.environ["MST_SUBTITLE_DOWNLOAD_PRIORITY"] = old_priority
            if old_babelarr_priority is not None:
                os.environ["BABELARR_SUBTITLE_DOWNLOAD_PRIORITY"] = old_babelarr_priority


if __name__ == "__main__":
    unittest.main()
