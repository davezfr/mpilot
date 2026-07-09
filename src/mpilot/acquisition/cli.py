from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Callable, TextIO

from mpilot.acquisition.client import DEFAULT_ACQUISITION_API_URL, AcquisitionApiClient, AcquisitionApiError
from mpilot.acquisition.env import env_first


ClientFactory = Callable[[argparse.Namespace], AcquisitionApiClient]


def build_parser(prog: str = "mpilot acquisition") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="CLI client for the MPilot acquisition REST API.",
    )
    parser.add_argument(
        "--api-url",
        default=env_first("QBITLARR_API_URL", default=DEFAULT_ACQUISITION_API_URL),
        help="MPilot acquisition API URL. Defaults to MPILOT_ACQUISITION_API_URL, QBITLARR_API_URL, or http://127.0.0.1:8000.",
    )
    parser.add_argument(
        "--api-key",
        default=env_first("QBITLARR_API_KEY"),
        help="Optional API key. Defaults to MPILOT_ACQUISITION_API_KEY or QBITLARR_API_KEY.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(env_first("QBITLARR_API_TIMEOUT_SECONDS", default="90") or "90"),
        help="HTTP timeout in seconds.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    handle_parser = subparsers.add_parser("handle", help="Handle a natural-language, IMDb, or title request.")
    handle_parser.add_argument("user_message", nargs="+", help="Request text, IMDb ID, IMDb URL, or title.")
    handle_parser.add_argument("--mode", choices=["auto", "manual", "confirm"], help="Override the default output mode.")
    handle_parser.add_argument("--save-path", help="Optional qBittorrent save path override.")
    handle_parser.add_argument("--user-id", help="Optional caller identifier for logs.")
    handle_parser.add_argument("--json", action="store_true", help="Print the raw JSON response.")

    search_parser = subparsers.add_parser("search", help="Search Prowlarr and print JSON results.")
    search_parser.add_argument("--identifier", help="Optional media identifier, such as imdb:tt0045877.")
    search_parser.add_argument("--query", help="Optional free-text query.")
    search_parser.add_argument("--category", type=int, action="append", dest="categories", help="Prowlarr category ID. Repeat for multiple categories.")
    search_parser.add_argument("--indexer-id", type=int, action="append", dest="indexer_ids", help="Prowlarr indexer ID. Repeat for multiple indexers.")

    download_parser = subparsers.add_parser("download", help="Queue a known download link.")
    download_parser.add_argument("download_link", help="Magnet, http(s), or bc download link.")
    download_parser.add_argument("--save-path", help="Optional qBittorrent save path override. Defaults to MPilot's inferred media path.")
    download_parser.add_argument("--query-id", help="Optional acquisition_handle query ID to preserve manual-result save-path context.")
    download_parser.add_argument("--user-id", help="Optional requester identifier used for torrent tagging.")

    downloads_parser = subparsers.add_parser("downloads", help="List qBittorrent downloads.")
    downloads_parser.add_argument("--user-id", help="Optional requester identifier used to filter tagged torrents.")
    downloads_parser.add_argument("--render", action="store_true", help="Print a chat-friendly progress message instead of raw JSON.")
    downloads_parser.add_argument("--watch", action="store_true", help="Repeat until interrupted.")
    downloads_parser.add_argument("--interval", type=float, default=5.0, help="Watch interval in seconds.")

    download_status_parser = subparsers.add_parser("download-status", help="Read one qBittorrent download by info hash.")
    download_status_parser.add_argument("info_hash", help="qBittorrent info hash.")
    download_status_parser.add_argument("--user-id", help="Optional requester identifier used to enforce tag filtering.")
    download_status_parser.add_argument("--render", action="store_true", help="Print a chat-friendly progress message instead of raw JSON.")

    health_parser = subparsers.add_parser("health", help="Check MPilot acquisition API health.")
    health_parser.add_argument("--deep", action="store_true", help="Also check Prowlarr and qBittorrent.")

    subparsers.add_parser("indexers", help="List configured Prowlarr indexers.")

    snapshot_parser = subparsers.add_parser("snapshot", help="Read a saved query snapshot.")
    snapshot_parser.add_argument("query_id", help="Query snapshot ID returned by handle.")

    return parser


def _default_client_factory(args: argparse.Namespace) -> AcquisitionApiClient:
    return AcquisitionApiClient(
        api_url=args.api_url,
        api_key=args.api_key,
        timeout=args.timeout,
    )


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    prog: str = "mpilot acquisition",
    client_factory: ClientFactory = _default_client_factory,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)
    client = client_factory(args)

    try:
        asyncio.run(_run_command(args, client, stdout))
    except AcquisitionApiError as exc:
        print(str(exc), file=stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


async def _run_command(args: argparse.Namespace, client: AcquisitionApiClient, stdout: TextIO) -> None:
    if args.command == "handle":
        payload = await client.handle(
            user_message=" ".join(args.user_message),
            user_id=args.user_id,
            save_path=args.save_path,
            mode=args.mode,
        )
        if args.json:
            _write_json(payload, stdout)
        else:
            print(_format_handle_response_for_humans(payload), file=stdout)
        return

    if args.command == "search":
        if not args.identifier and not args.query:
            raise AcquisitionApiError("search requires --identifier, --query, or both")
        _write_json(
            await client.search(
                identifier=args.identifier,
                query=args.query,
                categories=args.categories,
                indexer_ids=args.indexer_ids,
            ),
            stdout,
        )
        return

    if args.command == "download":
        _write_json(
            await client.download(
                args.download_link,
                save_path=args.save_path,
                query_id=args.query_id,
                user_id=args.user_id,
            ),
            stdout,
        )
        return

    if args.command == "downloads":
        if args.render:
            await _write_rendered_downloads(client, stdout, watch=args.watch, interval=args.interval, user_id=args.user_id)
            return
        await _write_downloads(client, stdout, watch=args.watch, interval=args.interval, user_id=args.user_id)
        return

    if args.command == "download-status":
        if args.render:
            payload = await client.render_download_status(args.info_hash, user_id=args.user_id)
            print(_rendered_message(payload), file=stdout)
        else:
            _write_json(await client.get_download_status(args.info_hash, user_id=args.user_id), stdout)
        return

    if args.command == "health":
        _write_json(await client.health(deep=args.deep), stdout)
        return

    if args.command == "indexers":
        _write_json(await client.list_prowlarr_indexers(), stdout)
        return

    if args.command == "snapshot":
        _write_json(await client.get_query_snapshot(args.query_id), stdout)
        return

    raise AcquisitionApiError(f"Unknown command: {args.command}")


async def _write_downloads(
    client: AcquisitionApiClient,
    stdout: TextIO,
    *,
    watch: bool,
    interval: float,
    user_id: str | None,
) -> None:
    while True:
        _write_json(await client.list_downloads(user_id=user_id), stdout)
        stdout.flush()
        if not watch:
            return
        await asyncio.sleep(interval)


async def _write_rendered_downloads(
    client: AcquisitionApiClient,
    stdout: TextIO,
    *,
    watch: bool,
    interval: float,
    user_id: str | None,
) -> None:
    while True:
        print(_rendered_message(await client.render_downloads_status(user_id=user_id)), file=stdout)
        stdout.flush()
        if not watch:
            return
        await asyncio.sleep(interval)


def _rendered_message(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if isinstance(message, str):
        return message
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _write_json(payload, stdout: TextIO) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False), file=stdout)


def _format_handle_response_for_humans(payload: dict[str, Any]) -> str:
    action = payload.get("action")
    message = _string_value(payload.get("message"))
    lines: list[str] = []
    if message:
        lines.append(message)
    elif action == "auto_download":
        title = _string_value(payload.get("title")) or "Download queued"
        quality = _string_value(payload.get("quality"))
        lines.append(f"{title} in {quality}" if quality else title)

    results = _result_list(payload.get("results"))
    alternatives = _result_list(payload.get("alternatives"))

    if action == "choose_title":
        candidates = _result_list(payload.get("candidates"))
        if candidates:
            if lines:
                lines.append("")
            for fallback_index, candidate in enumerate(candidates, start=1):
                index = candidate.get("index")
                if not isinstance(index, int):
                    index = fallback_index
                label = (
                    _string_value(candidate.get("label"))
                    or _string_value(candidate.get("title"))
                    or "Unknown title"
                )
                lines.append(f"{index}. {label}")
            lines.append("")
            lines.append("Pick one by its IMDb ID, e.g. `mpilot acquisition handle tt0045877`.")

    if action in ("show_results", "confirm"):
        if results:
            if lines:
                lines.append("")
            lines.extend(_format_numbered_results(results))
        elif action == "show_results":
            if lines:
                lines.append("")
            lines.append("No results.")

        if alternatives:
            if lines:
                lines.append("")
            lines.append("Alternatives:")
            lines.extend(_format_numbered_results(alternatives))

        if results or alternatives:
            lines.append("")
            lines.append("Use --json to inspect download links or pass a chosen link to `mpilot acquisition download`.")

    if lines:
        return "\n".join(lines)
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _format_numbered_results(results: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for fallback_index, result in enumerate(results, start=1):
        index = result.get("index")
        if not isinstance(index, int):
            index = fallback_index
        title = _string_value(result.get("title")) or "Untitled result"
        details = _result_details(result)
        lines.append(f"{index}. {title}")
        if details:
            lines.append(f"   {' | '.join(details)}")
    return lines


def _result_details(result: dict[str, Any]) -> list[str]:
    details: list[str] = []
    quality = _string_value(result.get("quality"))
    if quality:
        details.append(f"Quality: {quality}")
    seeders = result.get("seeders")
    if seeders is not None:
        details.append(f"Seeders: {seeders}")
    size = _format_size(result.get("size"))
    if size:
        details.append(f"Size: {size}")
    return details


def _format_size(value: Any) -> str | None:
    if value is None:
        return None
    try:
        size = float(value)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None

    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while size >= 1000 and unit_index < len(units) - 1:
        size /= 1000
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)} B"
    formatted = f"{size:.1f}".rstrip("0").rstrip(".")
    return f"{formatted} {units[unit_index]}"


def _result_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


if __name__ == "__main__":
    raise SystemExit(main())
