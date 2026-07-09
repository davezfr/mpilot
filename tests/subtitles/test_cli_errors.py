import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from babelarr import cli as cli_module
from babelarr.provider_policy import LowConfidenceSubtitleCandidatesError


class CliErrorTests(unittest.TestCase):
    def test_main_reports_config_errors_as_json(self):
        with patch.dict(os.environ, {"MST_NO_DOTENV": "1", "PLEX_BASE_URL": "", "PLEX_TOKEN": ""}):
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_module.main(["plex-resolve", "--rating-key", "101"])

        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["type"], "PlexConfigurationError")
        self.assertIn("PLEX_BASE_URL is required", payload["error"]["message"])
        self.assertIn("PLEX_BASE_URL is required", stderr.getvalue())

    def test_translate_plex_reports_low_confidence_as_structured_confirmation(self):
        class FakeResolver:
            def resolve(self, **_kwargs):
                return object()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(cli_module.PlexConnection, "from_values", return_value=object()), patch.object(
            cli_module.PathMapping, "from_values", return_value=object()
        ), patch.object(cli_module, "PlexApiClient", return_value=object()), patch.object(
            cli_module, "PlexResolver", return_value=FakeResolver()
        ), patch.object(
            cli_module,
            "translate_plex_resolved",
            side_effect=LowConfidenceSubtitleCandidatesError([], [], "medium"),
        ):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli_module.main(
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

        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "needs_confirmation")
        self.assertEqual(payload["proposal"]["action"], "confirm_low_confidence_subtitle")
        self.assertEqual(payload["proposal"]["message_key"], "low_confidence_subtitle_confirmation")
        self.assertNotRegex(payload["proposal"]["message"], r"[\u4e00-\u9fff]")
        self.assertEqual(payload["error"]["type"], "LowConfidenceSubtitleCandidatesError")
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
