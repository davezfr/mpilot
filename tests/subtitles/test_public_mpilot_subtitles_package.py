import subprocess
import sys
import unittest


class PublicMPilotSubtitlesPackageTests(unittest.TestCase):
    def test_mpilot_subtitles_package_exposes_cli_and_mcp_server(self):
        from mpilot.subtitles import cli, mcp_server

        self.assertEqual(cli.build_parser().prog, "mpilot subtitles")
        self.assertEqual(mcp_server.SERVER_NAME, "mpilot-subtitles")

    def test_python_module_entrypoint_uses_mpilot_subtitles_program_name(self):
        proc = subprocess.run(
            [sys.executable, "-m", "mpilot.subtitles", "--help"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertIn("usage: mpilot subtitles", proc.stdout)


if __name__ == "__main__":
    unittest.main()
