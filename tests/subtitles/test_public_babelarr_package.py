import subprocess
import sys
import unittest


class PublicBabelarrPackageTests(unittest.TestCase):
    def test_babelarr_package_exposes_cli_and_mcp_server(self):
        from babelarr import cli, mcp_server

        self.assertEqual(cli.build_parser().prog, "babelarr")
        self.assertEqual(mcp_server.SERVER_NAME, "babelarr")

    def test_python_module_entrypoint_uses_babelarr_program_name(self):
        proc = subprocess.run(
            [sys.executable, "-m", "babelarr", "--help"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        self.assertIn("usage: babelarr", proc.stdout)


if __name__ == "__main__":
    unittest.main()
