from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Sequence

from . import run_daemon


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mpilot-daemon",
        description="Run the unified MPilot background supervisor.",
    )
    parser.add_argument("--once", action="store_true", help="Run one supervisor cycle and exit.")
    parser.add_argument("--lock-path", type=Path, help="Single-instance lock path.")
    parser.add_argument("--interval-seconds", type=float, default=5.0, help="Loop sleep interval outside --once.")
    parser.add_argument("--runtime-store-dir", type=Path, help="Runtime workflow store directory.")
    parser.add_argument("--job-store-dir", type=Path, help="MPilot subtitle job store directory.")
    parser.add_argument("--no-acquisition", action="store_true", help="Disable acquisition download notification polling.")
    parser.add_argument(
        "--no-subtitle-notifications",
        action="store_true",
        help="Disable MPilot subtitle job notification polling.",
    )
    parser.add_argument("--no-runtime-dispatch", action="store_true", help="Disable Runtime ready-action dispatch.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)
    payload = run_daemon(
        once=args.once,
        lock_path=args.lock_path,
        interval_seconds=args.interval_seconds,
        runtime_store_dir=args.runtime_store_dir,
        job_store_dir=args.job_store_dir,
        run_acquisition=not args.no_acquisition,
        run_subtitle_notifications_step=not args.no_subtitle_notifications,
        run_runtime_dispatch=not args.no_runtime_dispatch,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if payload.get("status") == "already_running":
        return 2
    return 0 if payload.get("status") != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
