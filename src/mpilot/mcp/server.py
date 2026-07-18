from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from mpilot.acquisition.client import get_acquisition_client
from mpilot.mcp.acquisition_helpers import (
    _completion_metadata,
    _maybe_register_completion_watch,
    _prepare_agent_handle_payload,
    _resolve_notification_target,
    _start_notifier_if_pending,
)
from mpilot.mcp.acquisition_notifications import DownloadCompletionNotifier
from mpilot.runtime import mcp_server as runtime_tools
from mpilot.subtitles import mcp_server as subtitle_tools


SERVER_NAME = "mpilot"
SERVER_INSTRUCTIONS = (
    "Unified MPilot MCP server for media acquisition, subtitle automation, "
    "and cross-tool workflow coordination. Use media_request when the user asks "
    "to download media and optionally add subtitle processing in one request. "
    "For qbot: when acquisition_handle returns action=complementary_search, send its message first and then call "
    "acquisition_complementary_search with the returned query_id without waiting. Treat exact trimmed Telegram input "
    "补充搜索 as a control phrase for the active query_id; never pass it to acquisition_handle. "
    "Treat the exact clarify response 🔎 as the same deterministic complementary-search action. "
    "When a response contains message_key, localize that semantic key with message_params in the current "
    "conversation language; message is only an English fallback and must not determine the reply language."
)

logger = logging.getLogger("mpilot-mcp")
_download_completion_notifier_cache: DownloadCompletionNotifier | None = None


async def media_request(
    user_message: str,
    requester_id: str | None = None,
    subtitle_target_language: str | None = None,
    subtitle_source_language: str = "en",
    output_mode: str | None = None,
    notification_target: str | None = None,
    mode: str | None = None,
    save_path: str | None = None,
) -> dict[str, Any]:
    """Handle a media request and optionally attach subtitle intent.

    Without subtitle_target_language, this is equivalent to acquisition_handle.
    With subtitle_target_language, auto-download responses are recorded in the
    Runtime workflow store through record_acquisition_download_with_subtitle_intent.
    Clarify responses are passed through with continuation metadata so the
    caller can keep the subtitle intent after the user chooses a title/release.
    """
    subtitle_requested = _string_value(subtitle_target_language) is not None
    followup_message = "Download complete. Starting subtitle processing." if subtitle_requested else None
    payload = await get_acquisition_client().handle(
        user_message=user_message,
        user_id=requester_id,
        save_path=save_path,
        mode=mode,
    )
    if _completion_watch_requested(payload, notification_target, requester_id):
        await _maybe_register_completion_watch(
            _download_completion_notifier(),
            payload=payload,
            notification_target=notification_target,
            requester_id=requester_id,
            completion_followup_message=followup_message,
        )
    agent_payload = _prepare_agent_handle_payload(payload)
    if not subtitle_requested:
        return agent_payload

    intent = _subtitle_intent_payload(
        requester_id=requester_id,
        subtitle_source_language=subtitle_source_language,
        subtitle_target_language=subtitle_target_language,
        output_mode=output_mode,
        notification_target=notification_target,
        mode=mode,
        save_path=save_path,
    )
    info_hash = _payload_info_hash(payload)
    if payload.get("action") != "auto_download" or not info_hash:
        response = dict(agent_payload)
        response["subtitle_intent"] = {
            "status": "pending_user_selection",
            "intent": intent,
            "continue_with": {
                "tool": "media_request",
                "reason": "Call media_request again with the selected title or release and these subtitle fields.",
                "arguments": intent,
            },
        }
        return response

    response = dict(agent_payload)
    try:
        workflow = runtime_tools.record_qbitlarr_download_with_subtitle_intent(
            requester_id=_effective_requester_id(requester_id, notification_target),
            info_hash=info_hash,
            title=_payload_title(payload),
            imdb_id=_string_value(payload.get("imdb_id")),
            media_type=_string_value(payload.get("media_type")),
            season=_int_value(payload.get("season")),
            episode=_int_value(payload.get("episode")),
            progress=_payload_progress(payload),
            content_path=_payload_content_path(payload),
            source_language=_string_value(subtitle_source_language) or "en",
            target_language=_string_value(subtitle_target_language) or "",
            output_mode=_string_value(output_mode) or _default_output_mode(),
            notification_language=_string_value(subtitle_target_language),
            notification_target=_string_value(notification_target),
        )
        response["subtitle_intent"] = {
            "status": "registered",
            "workflow_id": workflow.get("workflow_id"),
            "workflow": workflow,
        }
    except Exception as error:
        response["subtitle_intent"] = {
            "status": "registration_failed",
            "intent": intent,
            "error": {"type": type(error).__name__, "message": str(error)},
        }
    return response


def create_mcp_server():
    from mpilot.core.dotenv import load_project_dotenv

    load_project_dotenv()
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as error:
        if error.name == "mcp":
            raise RuntimeError("MPilot MCP support requires: python3 -m pip install -e '.[mcp]'") from error
        raise

    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    if acquisition_tools_enabled():
        notifier = _download_completion_notifier()
        _start_notifier_if_pending(notifier)
        _register_acquisition_tools(mcp, notifier)
        mcp.tool()(media_request)
    if subtitle_tools_enabled():
        _register_tools(
            mcp,
            subtitle_tools.plex_search,
            subtitle_tools.subtitle_plan,
            subtitle_tools.job_create,
            subtitle_tools.job_create_video,
            subtitle_tools.job_start,
            subtitle_tools.job_show,
            subtitle_tools.job_run,
            subtitle_tools.job_resume,
            subtitle_tools.job_confirm_low_confidence,
            subtitle_tools.job_confirm_provider_fallback_language,
            subtitle_tools.job_prune,
        )
    if runtime_operator_tools_enabled():
        _register_tools(
            mcp,
            runtime_tools.record_acquisition_download,
            runtime_tools.record_acquisition_download_with_subtitle_intent,
            runtime_tools.attach_subtitle_intent,
            runtime_tools.record_local_video_subtitle_intent,
            runtime_tools.claim_ready_subtitle_job_create_video_actions,
            runtime_tools.record_subtitle_job_created,
            runtime_tools.record_subtitle_job_status,
            runtime_tools.queue_status,
            runtime_tools.workflow_show,
            runtime_tools.list_workflows,
        )
    return mcp


def acquisition_tools_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _env_truthy(env, "MPILOT_ENABLE_ACQUISITION_TOOLS") or _env_any(
        env,
        "MPILOT_ACQUISITION_API_URL",
        "MPILOT_PROWLARR_URL",
        "MPILOT_ACQUISITION_PROWLARR_URL",
        "MPILOT_PROWLARR_API_KEY",
        "MPILOT_ACQUISITION_PROWLARR_API_KEY",
        "MPILOT_QBIT_URL",
        "MPILOT_ACQUISITION_QBIT_URL",
        "MPILOT_QBIT_USERNAME",
        "MPILOT_ACQUISITION_QBIT_USERNAME",
        "MPILOT_QBIT_PASSWORD",
        "MPILOT_ACQUISITION_QBIT_PASSWORD",
        "QBITLARR_API_URL",
        "PROWLARR_URL",
        "PROWLARR_API_KEY",
        "QBIT_URL",
        "QBIT_USERNAME",
        "QBIT_PASSWORD",
    )


def subtitle_tools_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _env_truthy(env, "MPILOT_ENABLE_SUBTITLE_TOOLS") or _env_any(
        env,
        "PLEX_BASE_URL",
        "PLEX_TOKEN",
        "MPILOT_SUBTITLE_JOB_STORE_DIR",
        "MPILOT_SUBTITLE_BACKEND",
        "MPILOT_SUBTITLE_MODEL",
        "BABELARR_JOB_STORE_DIR",
        "MST_JOB_STORE_DIR",
        "BABELARR_BACKEND",
        "BABELARR_MODEL",
        "OPENSUBTITLES_API_KEY",
        "SUBDL_API_KEY",
    )


def runtime_operator_tools_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _env_truthy(env, "MPILOT_ENABLE_RUNTIME_OPERATOR_TOOLS")


def acquisition_control_tools_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return _env_truthy(env, "MPILOT_ENABLE_ACQUISITION_CONTROL_TOOLS")


def _register_tools(mcp: Any, *tools: Callable[..., Any]) -> None:
    for tool in tools:
        mcp.tool()(tool)


def _download_completion_notifier() -> DownloadCompletionNotifier:
    global _download_completion_notifier_cache
    if _download_completion_notifier_cache is None:
        _download_completion_notifier_cache = DownloadCompletionNotifier.from_env()
    return _download_completion_notifier_cache


def _completion_watch_requested(
    payload: dict[str, Any],
    notification_target: str | None,
    requester_id: str | None,
) -> bool:
    if not _resolve_notification_target(notification_target, requester_id):
        return False
    download_status = payload.get("download_status")
    if not isinstance(download_status, dict):
        return False
    info_hash = download_status.get("hash")
    return isinstance(info_hash, str) and bool(info_hash.strip())


def _register_acquisition_tools(mcp: Any, notifier: DownloadCompletionNotifier) -> None:
    @mcp.tool()
    async def acquisition_handle(
        user_message: str,
        user_id: str | None = None,
        save_path: str | None = None,
        mode: str | None = None,
        notification_target: str | None = None,
        completion_followup_message: str | None = None,
    ) -> dict[str, Any]:
        """Main MPilot acquisition tool for normal media download requests.

        Pass a raw request such as an IMDb ID, IMDb URL, supported Douban or
        AlloCine movie link, movie/show title, season phrase, or plain search
        terms. Every request is resolved to a single media identity first, then
        to ranked release choices, so keyword and canonical-ID paths end at the
        same place.

        Branch on the response "action":
          - "show_results": present ranked release choices and queue the
            user's pick with acquisition_download. When "agent_clarify" is
            present, ask an open-ended clarify question that includes
            agent_clarify.display_table in a monospace/code block. If
            agent_clarify.display_notice is present, append it as short notice
            text. Pass agent_clarify.choices unchanged as the button labels and
            use agent_clarify.response_mapping to map the selected response.
            A 🔎 mapping means call acquisition_complementary_search with its
            query_id; it is not a release selection. Do not pass table rows as
            clarify choices or append another numbered list.
          - "auto_download" (mode="auto" only): the best release was queued;
            an "alternatives" list of runner-ups is included for "did you
            mean..." follow-up.
          - "confirm" (mode="confirm"): the top pick plus alternatives, with
            nothing queued.
          - "choose_title": a keyword matched several titles. If
            "agent_clarify" is present, ask an open-ended clarify question with
            agent_clarify.display_table in a monospace/code block, pass
            agent_clarify.choices as numeric button labels, and use
            agent_clarify.response_mapping to find the candidate index. After
            the user picks a title, call acquisition_handle again with that
            candidate's imdb_id.
          - "needs_imdb": no title could be identified. Relay the message and
            ask the user to send an IMDb link or ID.
          - "complementary_search": qbot must first send the returned message
            meaning to Telegram in the current conversation language, using
            message_key/message_params when present. Then immediately call
            acquisition_complementary_search with the returned query_id without
            waiting for another user reply. Do not send message verbatim merely
            because it is populated; it is an English fallback.

        Args:
            user_message: Media link, canonical ID, title, season phrase, or
                search terms. Include "4K", "2160p", "UHD", "720p", "480p",
                or "Remux" only when the user explicitly asks for that quality.
            user_id: Optional stable caller identifier. In shared bots, pass a
                stable chat/user ID so future status checks can be requester
                scoped.
            save_path: Optional qBittorrent save-path override. Leave unset to
                use MPilot acquisition defaults.
            mode: Optional output mode: "manual" returns ranked choices,
                "auto" queues the best release, and "confirm" returns the top
                pick without queueing.
            notification_target: Optional Hermes send target such as
                "telegram:123456789". When set and the response includes a
                torrent hash, MPilot sends a one-time completion notification.
                When omitted, user_id is reused if it already looks like a
                Hermes send target.
            completion_followup_message: Optional one-line message appended to
                the completion notification when a downstream workflow was
                already requested.
        """
        payload = await get_acquisition_client().handle(
            user_message=user_message,
            user_id=user_id,
            save_path=save_path,
            mode=mode,
        )
        await _maybe_register_completion_watch(
            notifier,
            payload=payload,
            notification_target=notification_target,
            requester_id=user_id,
            completion_followup_message=completion_followup_message,
        )
        return _prepare_agent_handle_payload(payload)

    @mcp.tool()
    async def acquisition_get_query_snapshot(query_id: str) -> dict[str, Any]:
        """Return the saved search snapshot for a previous acquisition_handle query_id.

        Use this only when the user asks for saved context from the same query.
        The snapshot may include an independent complementary title/year entry.
        Do not expose raw download_link values unless the user
        explicitly asks to queue or inspect a specific result.
        """
        return await get_acquisition_client().get_query_snapshot(query_id)

    @mcp.tool()
    async def acquisition_complementary_search(query_id: str) -> dict[str, Any]:
        """Run MPilot's manual-only complementary title/year search for an existing query.

        qbot protocol: when acquisition_handle returns action=complementary_search,
        first localize and send that response's message_key/message_params to Telegram,
        then call this tool automatically with the same query_id without waiting for user
        confirmation. Localize this tool's message_key/message_params again when rendering
        results; message is only an English fallback. Also call this tool
        when the user enters the exact control phrase 补充搜索 after trimming surrounding
        whitespace, or chooses the exact 🔎 clarify response, and the current conversation
        or Topic has an active query_id. Never send either control value to acquisition_handle.
        If there is no active query_id, tell the user
        to search for a movie or show first and do not call either search tool. Render
        returned choices like normal manual release choices; they are title/year validated
        candidates and are explicitly not IMDb-ID verified. Never auto-download them.
        """
        payload = await get_acquisition_client().complementary_search(query_id)
        return _prepare_agent_handle_payload(payload)

    @mcp.tool()
    async def acquisition_search(
        identifier: str | None = None,
        query: str | None = None,
        categories: list[int] | None = None,
        indexer_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Search torrent indexers via Prowlarr and return ranked results.

        Manual workflow helper. Prefer acquisition_handle for normal user
        requests. Call this to discover candidates, then pass the chosen
        result's download_link to acquisition_download.

        Args:
            identifier: Typed media ID or URL for precise matching. Supported
                prefixes include imdb:, tmdb:, tvdb:, tvmaze:, trakt:, and
                douban:. A bare IMDb ID also works.
            query: Free-text keywords, such as "The General 1926 BluRay 1080p".
                Combine with identifier for best results.
            categories: Optional Prowlarr category IDs, such as 2000 for movies
                or 5000 for TV. Leave unset for MPilot defaults.
            indexer_ids: Optional Prowlarr indexer IDs. Use
                acquisition_list_indexers to discover them.

        At least one argument is required. Returns up to 20 results. Each result
        contains title, download_link, size, seeders, leechers, indexer,
        protocol, publish_date, and info_hash.
        """
        return await get_acquisition_client().search(
            identifier=identifier,
            query=query,
            categories=categories,
            indexer_ids=indexer_ids,
        )

    @mcp.tool()
    async def acquisition_download(
        download_link: str,
        save_path: str | None = None,
        query_id: str | None = None,
        notification_target: str | None = None,
        requester_id: str | None = None,
        completion_followup_message: str | None = None,
    ) -> dict[str, Any]:
        """Queue a torrent or magnet link in qBittorrent.

        Pass a download_link from acquisition_search results or an
        acquisition_handle manual selection. For acquisition_handle choices,
        query_id is required and the selected result must have passed MPilot's
        snapshot verification gate. Accepted schemes are http, https direct
        .torrent files, magnet, and bc.

        On success, send returned rendered_status verbatim as a status message
        so the user sees MPilot's prepared progress card with size, speed, and
        ETA. If rendered_status_buttons is present, attach those inline buttons
        to the same status message without changing callback_data. Do not
        reconstruct the bar or buttons from raw download_status fields.

        Args:
            download_link: A download_link value from acquisition_search,
                acquisition_handle, or acquisition_get_query_snapshot.
            save_path: Optional qBittorrent save-path override. Leave unset to
                use MPilot's inferred media path.
            query_id: Query ID previously returned by acquisition_handle.
                Required when the user chooses a numbered result from
                acquisition_handle or acquisition_get_query_snapshot so MPilot
                can authorize the verified snapshot result and preserve the
                original movie/TV save-path context.
            notification_target: Optional Hermes send target such as
                "telegram:123456789". When set and MPilot can identify the
                torrent hash, the requester gets a one-time completion notice.
                When omitted, requester_id is reused if it already looks like a
                Hermes send target.
            requester_id: Optional stable user identifier. When set, MPilot tags
                the torrent for requester-scoped status queries and stores the
                same identifier with the completion watch.
            completion_followup_message: Optional one-line message appended to
                the completion notification when a downstream workflow was
                already requested.
        """
        payload = await get_acquisition_client().download(
            download_link,
            save_path=save_path,
            query_id=query_id,
            user_id=requester_id,
        )
        await _maybe_register_completion_watch(
            notifier,
            payload=payload,
            notification_target=notification_target,
            requester_id=requester_id,
            completion_followup_message=completion_followup_message,
        )
        return payload

    @mcp.tool()
    async def acquisition_list_downloads(requester_id: str | None = None) -> list[dict[str, Any]]:
        """List qBittorrent torrents currently tracked by MPilot acquisition.

        Returns each torrent's name, state, progress, size, seeds, and info
        hash. When requester_id is set, only torrents tagged for that requester
        are returned. Use a stable chat/user identifier for shared bots.
        """
        return await get_acquisition_client().list_downloads(user_id=requester_id)

    @mcp.tool()
    async def acquisition_get_download_status(info_hash: str, requester_id: str | None = None) -> dict[str, Any]:
        """Read one qBittorrent torrent by info hash.

        Prefer this over acquisition_list_downloads when you already have the
        exact torrent hash from a previous auto-download response. When
        requester_id is set, the torrent must be tagged for that requester.
        """
        return await get_acquisition_client().get_download_status(info_hash, user_id=requester_id)

    @mcp.tool()
    async def acquisition_render_downloads_status(requester_id: str | None = None) -> dict[str, Any]:
        """Render current downloads as a chat-ready progress message.

        Use this when a Telegram or chat user asks what is downloading or asks
        to show download status. Send the response message verbatim. If
        watch_policy is present and the user asks to keep tracking, keep the
        progress bar in a separate status message, refresh that same message at
        watch_policy.update_interval_seconds, and stop after
        watch_policy.max_duration_seconds with watch_policy.timeout_message.

        Completion notifications are separate one-time watches; do not merge
        them into the dynamic progress card.
        """
        return await get_acquisition_client().render_downloads_status(user_id=requester_id)

    @mcp.tool()
    async def acquisition_render_download_status(info_hash: str, requester_id: str | None = None) -> dict[str, Any]:
        """Render one qBittorrent torrent as a chat-ready progress message.

        Prefer this when a previous acquisition_handle or acquisition_download
        response included an exact torrent hash. Send the returned message
        verbatim and follow watch_policy exactly for bounded dynamic refreshes.
        Completion notifications remain a separate one-time watch.
        """
        return await get_acquisition_client().render_download_status(info_hash, user_id=requester_id)

    async def acquisition_pause_download(info_hash: str, requester_id: str) -> dict[str, Any]:
        """Pause a qBittorrent torrent owned by the requester.

        This is for direct Telegram callback handling, not normal chat planning.
        requester_id is required and must be the stable user ID that originally
        queued the download. If the torrent is not tagged for that requester,
        MPilot returns not found.
        """
        return await get_acquisition_client().pause_download(info_hash, user_id=requester_id)

    async def acquisition_resume_download(info_hash: str, requester_id: str) -> dict[str, Any]:
        """Resume a qBittorrent torrent owned by the requester.

        This is for direct Telegram callback handling. requester_id is required
        and must match the torrent's requester tag.
        """
        return await get_acquisition_client().resume_download(info_hash, user_id=requester_id)

    async def acquisition_delete_download(info_hash: str, requester_id: str) -> dict[str, Any]:
        """Delete a qBittorrent torrent task owned by the requester.

        This removes the qBittorrent task while keeping downloaded files. Use
        only after the Telegram adapter has shown an explicit confirmation.
        requester_id is required and must match the torrent's requester tag.
        """
        return await get_acquisition_client().delete_download(info_hash, user_id=requester_id)

    if acquisition_control_tools_enabled():
        _register_tools(
            mcp,
            acquisition_pause_download,
            acquisition_resume_download,
            acquisition_delete_download,
        )

    @mcp.tool()
    async def acquisition_watch_download(
        info_hash: str,
        notification_target: str,
        title: str | None = None,
        imdb_id: str | None = None,
        media_type: str | None = None,
        requester_id: str | None = None,
        completion_followup_message: str | None = None,
    ) -> dict[str, Any]:
        """Send a one-time completion notification for a torrent hash.

        Use this when a user asks to be notified when a specific torrent
        finishes. notification_target must be a Hermes send target such as
        "telegram:123456789" so the notification goes back to the requesting
        chat/user.
        """
        watch = await notifier.register_watch(
            info_hash=info_hash,
            title=title or info_hash,
            notification_target=notification_target,
            metadata=_completion_metadata(
                imdb_id=imdb_id,
                media_type=media_type,
                completion_followup_message=completion_followup_message,
            ),
            requester_id=requester_id,
        )
        return {"status": "watching", "watch": watch}

    @mcp.tool()
    async def acquisition_health(deep: bool = False) -> dict[str, Any]:
        """Check whether the MPilot acquisition API is reachable.

        Returns service readiness. Set deep=true to also check Prowlarr and
        qBittorrent dependencies.
        """
        return await get_acquisition_client().health(deep=deep)

    @mcp.tool()
    async def acquisition_list_indexers() -> list[dict[str, Any]]:
        """List configured Prowlarr indexers, IDs, and IMDb search modes.

        Use this when setting primary/fallback IDs or classifying an indexer as
        native, keyword, or disabled for IMDb-only acquisition. Each item also
        reports whether Prowlarr advertises native imdbid support. Unconfigured
        indexers are skipped once strict IMDb routing is enabled.
        """
        return await get_acquisition_client().list_prowlarr_indexers()


def _subtitle_intent_payload(
    *,
    requester_id: str | None,
    subtitle_source_language: str,
    subtitle_target_language: str | None,
    output_mode: str | None,
    notification_target: str | None,
    mode: str | None,
    save_path: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requester_id": requester_id,
        "subtitle_source_language": _string_value(subtitle_source_language) or "en",
        "subtitle_target_language": _string_value(subtitle_target_language),
        "output_mode": _string_value(output_mode) or _default_output_mode(),
        "notification_target": notification_target,
        "mode": mode,
        "save_path": save_path,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _payload_info_hash(payload: dict[str, Any]) -> str | None:
    download_status = payload.get("download_status")
    if isinstance(download_status, dict):
        value = _string_value(download_status.get("hash"))
        if value:
            return value
    return _string_value(payload.get("info_hash"))


def _payload_title(payload: dict[str, Any]) -> str | None:
    download_status = payload.get("download_status")
    if isinstance(download_status, dict):
        return _string_value(payload.get("title")) or _string_value(download_status.get("name"))
    return _string_value(payload.get("title"))


def _payload_progress(payload: dict[str, Any]) -> float:
    download_status = payload.get("download_status")
    if isinstance(download_status, dict):
        try:
            return float(download_status.get("progress", 0.0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _payload_content_path(payload: dict[str, Any]) -> str | None:
    download_status = payload.get("download_status")
    if isinstance(download_status, dict):
        return _string_value(download_status.get("content_path"))
    return None


def _effective_requester_id(requester_id: str | None, notification_target: str | None) -> str:
    return _string_value(requester_id) or _string_value(notification_target) or "mpilot"


def _default_output_mode() -> str:
    return (
        _string_value(os.getenv("MPILOT_SUBTITLE_OUTPUT_MODE"))
        or _string_value(os.getenv("BABELARR_OUTPUT_MODE"))
        or _string_value(os.getenv("SUBTRANS_OUTPUT_MODE"))
        or "bilingual-ass"
    )


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _int_value(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _env_any(env: dict[str, str], *names: str) -> bool:
    return any(_string_value(env.get(name)) for name in names)


def _env_truthy(env: dict[str, str], name: str) -> bool:
    value = _string_value(env.get(name))
    return value is not None and value.casefold() in {"1", "true", "yes", "on"}


async def run_mcp_server() -> None:
    server = create_mcp_server()
    await server.run_stdio_async()


def main() -> None:
    try:
        asyncio.run(run_mcp_server())
    except KeyboardInterrupt:
        logger.info("MPilot MCP server stopped")


if __name__ == "__main__":
    main()
