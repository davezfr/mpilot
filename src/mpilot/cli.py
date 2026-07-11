from __future__ import annotations

import argparse
import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="mpilot")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("acquisition", help="Run the acquisition CLI")
    subparsers.add_parser("subtitles", help="Run the subtitle CLI")
    subparsers.add_parser("runtime", help="Run the workflow runtime CLI")

    if not args or args[0] in ("-h", "--help"):
        parser.parse_args(args)
        return 0

    command, remainder = args[0], args[1:]
    if command == "acquisition":
        from mpilot.acquisition.cli import main as acquisition_main

        return acquisition_main(remainder, prog="mpilot acquisition")
    if command == "subtitles":
        from mpilot.subtitles.cli import main as subtitles_main

        return subtitles_main(remainder, prog="mpilot subtitles")
    if command == "runtime":
        from mpilot.runtime.cli import main as runtime_main

        return runtime_main(remainder, prog="mpilot runtime")
    parser.error("unknown command: %s" % command)
    return 2
