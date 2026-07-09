from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from mpilot.acquisition.client import get_qbitlarr_client
from mpilot.acquisition.domain.choice_table import render_choice_table, render_title_choice_table
from mpilot.acquisition.models import ManualSearchResult, MovieCandidate
from mpilot.core.targets import resolve_notification_target
from mpilot.mcp.qbitlarr_notifications import DownloadCompletionNotifier


logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING").upper())
logger = logging.getLogger("qbitlarr-mcp")

def create_mcp_server() -> FastMCP:
    notifier = DownloadCompletionNotifier.from_env()
    _start_notifier_if_pending(notifier)
    mcp = FastMCP(
        "qbitlarr",
        instructions=(
            "Search torrent indexers via Prowlarr and manage qBittorrent downloads. "
            "Use this only for media the user is allowed to access. "
            "Default workflow: call qbitlarr_handle with a user's IMDb, Douban, or AlloCine movie link, ID, or title; "
            "it returns ranked choices by default (mode='manual') — present them and download the user's pick. "
            "For IMDb IDs, IMDb URLs, and supported Douban or AlloCine movie links, qBitlarr auto-selects a 1080p release by default and queues it. "
            "For keywords, qBitlarr returns a friendly numbered candidate list. "
            "qbitlarr_handle returns query_id when a saved query snapshot is available. "
            "Use qbitlarr_search and qbitlarr_download only when manual control is needed. "
            "Use qbitlarr_render_downloads_status or qbitlarr_render_download_status when a user asks for download status; "
            "these return a chat-ready emoji progress card and a bounded 3-second dynamic-refresh policy. "
            "Use qbitlarr_pause_download, qbitlarr_resume_download, and qbitlarr_delete_download only for Telegram "
            "download-control callbacks, always passing the current Telegram user as requester_id. "
            "Use qbitlarr_list_prowlarr_indexers to discover indexer IDs for configuration."
        ),
    )

    @mcp.tool()
    async def qbitlarr_handle(
        user_message: str,
        user_id: str | None = None,
        save_path: str | None = None,
        mode: str | None = None,
        notification_target: str | None = None,
        completion_followup_message: str | None = None,
    ) -> dict[str, Any]:
        """Main qBitlarr tool for normal users.

        Pass a raw user request such as "tt0045877",
        "https://www.imdb.com/title/tt0045877/",
        "https://movie.douban.com/subject/1292052/",
        "https://www.allocine.fr/film/fichefilm_gen_cfilm=25801.html",
        "The Hitch-Hiker", or "Example Show S03". Every request is resolved to a
        single movie/show identity first, then to ranked release choices, so the
        keyword path and the IMDb/Douban/AlloCine path end at the same place.

        Branch on the response "action":
          - "show_results": ranked release choices for one identified title.
            Present them and download the user's pick. The MCP tool returns
            "agent_clarify" for this action. When it is present, ask an
            open-ended clarify question that includes agent_clarify.display_table
            in a monospace/code block. If agent_clarify.display_notice is
            present, append it after the block as ordinary short notice text.
            Pass agent_clarify.choices as the short numeric button labels. Use
            agent_clarify.response_mapping to map the selected number back to a
            result index, then queue that result. Do not pass table rows as
            clarify choices or append another numbered list.
          - "auto_download" (mode="auto" only): the best release was queued; an
            "alternatives" list of runner-ups is included so the user can be
            offered "or did you mean..." without a second call.
          - "confirm" (mode="confirm"): the top pick plus alternatives, nothing
            queued.
          - "choose_title": a keyword matched several titles. The "candidates"
            list holds {index, title, year, imdb_id, label}. If
            "agent_clarify" is present, ask an open-ended clarify question with
            agent_clarify.display_table in a monospace/code block, pass
            agent_clarify.choices as short numeric button labels, and use
            agent_clarify.response_mapping to find the
            candidate index. After the user picks a title, call qbitlarr_handle
            again with the chosen candidate's imdb_id to get its release choices.
          - "needs_imdb": no title could be identified (an unresolved link or a
            keyword with no match). Relay the message and ask the user to send
            an IMDb link or ID.

        Args:
            user_message: IMDb ID, IMDb URL, supported Douban movie link,
                supported AlloCine film link, title, season phrase, or plain
                search terms. qBitlarr recommends 1080p by default. Include
                "4K", "2160p", "UHD", "720p", "480p", or "Remux" only when
                the user explicitly asks for that quality.
            user_id: Optional caller identifier for logs and multi-user context.
                When serving multiple people through one qBittorrent instance,
                pass a stable chat/user ID so future status checks can be scoped
                to that requester's tagged torrents.
            save_path: Optional qBittorrent save path override, such as
                "/media/Kids". Leave unset to use qBitlarr's configured defaults.
            mode: Optional output mode. "manual" (server default) returns a
                ranked list without downloading; "auto" picks and queues the
                best release immediately; "confirm" returns the top pick and
                alternatives without queueing. Leave unset to use the server
                default (QBITLARR_DEFAULT_MODE).
            notification_target: Optional Hermes send target, such as
                "telegram:123456789". When set and the response includes a
                torrent hash, qBitlarr will send that target a one-time
                completion notification when the torrent reaches 100%. When
                omitted, qBitlarr reuses user_id if it already looks like a
                Hermes send target.
            completion_followup_message: Optional one-line user-facing message
                appended to the completion notification, such as "Download
                complete. Starting subtitle processing." Use this only when a
                downstream workflow was already requested.
        """
        payload = await get_qbitlarr_client().handle(
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
    async def qbitlarr_get_query_snapshot(query_id: str) -> dict[str, Any]:
        """Return the saved search snapshot document for a previous qbitlarr_handle query_id.

        Use this only when the user asks for more alternatives from the same
        query. The snapshot may include a later fallback pass from slower
        fallback indexers. Do not expose raw download_link values unless
        the user explicitly asks to queue or inspect a specific result.
        """
        return await get_qbitlarr_client().get_query_snapshot(query_id)

    @mcp.tool()
    async def qbitlarr_search(
        identifier: str | None = None,
        query: str | None = None,
        categories: list[int] | None = None,
        indexer_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Search torrent indexers via Prowlarr and return ranked results.

        Manual workflow helper. Prefer qbitlarr_handle for normal user requests.
        Call this to discover candidates, then pass the chosen result's
        download_link to qbitlarr_download.

        Args:
            identifier: Typed media ID or IMDb URL for precise matching. Supported prefixes:
                imdb:tt0045877, tmdb:123456, tvdb:123456, tvmaze:123456,
                trakt:1234, douban:1234567. A bare IMDb ID (tt0045877) also works.
            query: Free-text keywords, e.g. "The General 1926 BluRay 1080p".
                Combine with identifier for best results.
            categories: Optional Prowlarr category IDs, such as 2000 for movies
                or 5000 for TV. Leave unset for qBitlarr defaults.
            indexer_ids: Optional Prowlarr indexer IDs. Use
                qbitlarr_list_prowlarr_indexers to discover them.

        At least one argument is required. Returns up to 20 results. Each result
        contains title, download_link (pass to qbitlarr_download), size (bytes),
        seeders, leechers, indexer, protocol (torrent/usenet), publish_date,
        and info_hash.
        """
        return await get_qbitlarr_client().search(
            identifier=identifier,
            query=query,
            categories=categories,
            indexer_ids=indexer_ids,
        )

    @mcp.tool()
    async def qbitlarr_download(
        download_link: str,
        save_path: str | None = None,
        query_id: str | None = None,
        notification_target: str | None = None,
        requester_id: str | None = None,
        completion_followup_message: str | None = None,
    ) -> dict[str, Any]:
        """Queue a torrent or magnet link in qBittorrent.

        Pass a download_link from qbitlarr_search results. Accepted schemes:
        http, https (direct .torrent file), magnet, bc (BitComet).

        Returns {"status": "success", "message": "Download queued",
        "rendered_status": "<chat-ready progress card>"} on success. Send
        rendered_status verbatim as a status message so the user sees the
        10-cell emoji progress bar with size, speed, and ETA. If
        rendered_status_buttons is present, attach those inline buttons to the
        same status message without changing callback_data. Do NOT reconstruct
        the bar or buttons from raw download_status fields.

        Args:
            download_link: A download_link value returned by qbitlarr_search.
            save_path: Optional qBittorrent save path override, such as
                "/media/Kids". Leave unset to use qBitlarr's inferred default
                media path.
            query_id: Optional query ID previously returned by qbitlarr_handle.
                When the user is choosing a numbered result from qbitlarr_handle
                or qbitlarr_get_query_snapshot, pass that same query_id here so
                qBitlarr can preserve the original movie/TV save-path context.
            notification_target: Optional Hermes send target, such as
                "telegram:123456789". When set and qBitlarr can identify the
                torrent hash, the requester gets a one-time completion notice.
                When omitted, qBitlarr reuses requester_id if it already
                looks like a Hermes send target.
            requester_id: Optional stable user identifier. When set, qBitlarr
                tags the torrent for requester-scoped status queries and stores
                the same identifier with the completion watch.
            completion_followup_message: Optional one-line user-facing message
                appended to the completion notification, such as "Download
                complete. Starting subtitle processing." Use this only when a
                downstream workflow was already requested.
        """
        payload = await get_qbitlarr_client().download(
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
    async def qbitlarr_list_downloads(requester_id: str | None = None) -> list[dict[str, Any]]:
        """List all torrents currently tracked by qBittorrent.

        Returns each torrent's name, state, progress (0.0–1.0), size in bytes,
        number of seeds, and info hash. Call this after qbitlarr_download to confirm
        the torrent was accepted and to monitor its progress.

        When requester_id is set, only torrents tagged for that requester are
        returned. Use a stable chat/user identifier here for multi-user bots.

        Common state values: downloading, uploading (seeding), stalledDL,
        stalledUP, pausedDL, pausedUP, metaDL (fetching metadata), checkingDL.
        """
        return await get_qbitlarr_client().list_downloads(user_id=requester_id)

    @mcp.tool()
    async def qbitlarr_get_download_status(info_hash: str, requester_id: str | None = None) -> dict[str, Any]:
        """Read one qBittorrent torrent by info hash.

        Prefer this over qbitlarr_list_downloads when you already have the
        exact torrent hash from a previous qbitlarr_handle auto-download
        response and want a reliable follow-up status check. When requester_id
        is set, the torrent must already be tagged for that requester.
        """
        return await get_qbitlarr_client().get_download_status(info_hash, user_id=requester_id)

    @mcp.tool()
    async def qbitlarr_render_downloads_status(requester_id: str | None = None) -> dict[str, Any]:
        """Render current qBittorrent downloads as a chat-ready progress message.

        Use this when a Telegram or chat user asks "what's downloading?" or
        "show download status". The response contains:
        - message: text ready to send to the chat, including progress bars.
        - watch_policy: guidance for optional dynamic tracking. If the user
          asks to keep tracking, keep the progress bar in a separate status
          message and refresh that same status message
          every watch_policy.update_interval_seconds and stop after
          watch_policy.max_duration_seconds. If the torrent is still active at
          timeout, edit the message one last time with watch_policy.timeout_message.

        Completion notifications are separate from this dynamic status card;
        keep using qbitlarr_watch_download or qbitlarr_handle notification
        targets for the final "download complete" message.

        Args:
            requester_id: Optional stable requester identifier. Use
                "telegram:<current user id>" in shared-bot setups so the
                rendered status only includes that user's tagged torrents.
        """
        return await get_qbitlarr_client().render_downloads_status(user_id=requester_id)

    @mcp.tool()
    async def qbitlarr_render_download_status(info_hash: str, requester_id: str | None = None) -> dict[str, Any]:
        """Render one qBittorrent torrent as a chat-ready progress message.

        Prefer this when a previous qbitlarr_handle or qbitlarr_download
        response included an exact torrent hash. The response contains
        message and watch_policy fields for the same bounded dynamic status
        card behavior as qbitlarr_render_downloads_status.

        For background tracking, keep the progress bar in a separate status
        message and refresh that same status message every
        watch_policy.update_interval_seconds. Stop that loop after 15 minutes
        even if the download is still running. Completion notifications remain
        a separate one-time watch.

        Args:
            info_hash: qBittorrent info hash to render.
            requester_id: Optional stable requester identifier. When set, the
                torrent must already be tagged for that requester.
        """
        return await get_qbitlarr_client().render_download_status(info_hash, user_id=requester_id)

    @mcp.tool()
    async def qbitlarr_pause_download(info_hash: str, requester_id: str) -> dict[str, Any]:
        """Pause a qBittorrent torrent owned by the requester.

        This is for direct Telegram callback handling, not normal chat
        planning. requester_id is required and must be the stable user ID that
        originally queued the download, such as "telegram:123456789". If the
        torrent is not tagged for that requester, qBitlarr returns not found.
        """
        return await get_qbitlarr_client().pause_download(info_hash, user_id=requester_id)

    @mcp.tool()
    async def qbitlarr_resume_download(info_hash: str, requester_id: str) -> dict[str, Any]:
        """Resume a qBittorrent torrent owned by the requester.

        This is for direct Telegram callback handling. requester_id is required
        and must match the torrent's requester tag.
        """
        return await get_qbitlarr_client().resume_download(info_hash, user_id=requester_id)

    @mcp.tool()
    async def qbitlarr_delete_download(info_hash: str, requester_id: str) -> dict[str, Any]:
        """Delete a qBittorrent torrent task owned by the requester.

        This removes the qBittorrent task while keeping downloaded files. Use
        only after the Telegram adapter has shown an explicit confirmation.
        requester_id is required and must match the torrent's requester tag.
        """
        return await get_qbitlarr_client().delete_download(info_hash, user_id=requester_id)

    @mcp.tool()
    async def qbitlarr_watch_download(
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

        Args:
            info_hash: qBittorrent info hash to watch.
            notification_target: Hermes send target, such as "telegram:123456789".
            title: Optional friendly title for the completion message.
            imdb_id: Optional canonical IMDb ID to include in the completion message.
            media_type: Optional media type metadata, such as "movie" or "tv".
            requester_id: Optional stable user identifier associated with the watch.
            completion_followup_message: Optional one-line user-facing message
                appended to the completion notification when a downstream
                workflow was already requested.
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
    async def qbitlarr_health(deep: bool = False) -> dict[str, Any]:
        """Check whether the qBitlarr API is reachable.

        Returns {"status": "ok", "service": "qBitlarr API"} when the API is
        reachable. Set deep=true to also check Prowlarr and qBittorrent.

        Args:
            deep: When true, also check Prowlarr and qBittorrent readiness.
        """
        return await get_qbitlarr_client().health(deep=deep)

    @mcp.tool()
    async def qbitlarr_list_prowlarr_indexers() -> list[dict[str, Any]]:
        """List configured Prowlarr indexers and their numeric IDs.

        Use this when setting PROWLARR_PRIMARY_INDEXER_IDS or
        PROWLARR_FALLBACK_INDEXER_IDS. Each item includes id, name, enabled,
        and protocol when Prowlarr provides those fields.
        """
        return await get_qbitlarr_client().list_prowlarr_indexers()

    return mcp


async def _maybe_register_completion_watch(
    notifier: DownloadCompletionNotifier,
    *,
    payload: dict[str, Any],
    notification_target: str | None,
    requester_id: str | None,
    completion_followup_message: str | None = None,
) -> None:
    resolved_target = _resolve_notification_target(notification_target, requester_id)
    if not resolved_target:
        return

    download_status = payload.get("download_status")
    if not isinstance(download_status, dict):
        return

    info_hash = download_status.get("hash")
    if not isinstance(info_hash, str) or not info_hash.strip():
        return

    title = _string_value(payload.get("title")) or _string_value(download_status.get("name")) or info_hash
    watch = await notifier.register_watch(
        info_hash=info_hash,
        title=title,
        notification_target=resolved_target,
        metadata=_completion_metadata_from_payload(
            payload,
            completion_followup_message=completion_followup_message,
        ),
        requester_id=requester_id,
        track_progress=True,
        start=False,
    )
    notifier.start()
    _suppress_progress_watch_message(payload)
    payload["notification_watch"] = {"status": "watching", "watch": watch}
    payload["progress_watch"] = {"status": "tracking", "watch": watch}


def _string_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


_RAW_CHOICE_RENDER_FIELDS = {
    "choices_table",
    "choice_display",
    "choice_buttons",
    "choice_rich_message",
    "ui_hints",
}

_AGENT_CLARIFY_MAX_ROWS = 4
_RELEASE_CLARIFY_DISPLAY_NOTICE = (
    "• 🧲: Seed activity; more seeders usually download faster.\n"
    "• 💾: File size; smaller files usually download faster."
)


def _prepare_agent_handle_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce picker payloads to fields a model can safely pass to clarify."""
    action = payload.get("action")
    if action not in {"show_results", "choose_title"}:
        return payload

    agent_payload = dict(payload)
    for field in _RAW_CHOICE_RENDER_FIELDS:
        agent_payload.pop(field, None)

    clarify_payload = _agent_clarify_payload(payload)
    if clarify_payload:
        agent_payload["agent_clarify"] = clarify_payload
    return agent_payload


def _agent_clarify_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    action = payload.get("action")
    if action == "show_results":
        display_table, response_mapping = _release_clarify_display(payload.get("results"))
        question = "Choose a version to download:"
    elif action == "choose_title":
        display_table, response_mapping = _title_clarify_display(payload.get("candidates"))
        question = "Choose a title:"
    else:
        return None

    if not display_table or not response_mapping:
        return None
    clarify_payload = {
        "question": question,
        "display_table": display_table,
        "choices": [str(item["choice"]) for item in response_mapping],
        "response_mapping": response_mapping,
    }
    if action == "show_results":
        clarify_payload["display_notice"] = _RELEASE_CLARIFY_DISPLAY_NOTICE
    return clarify_payload


def _release_clarify_display(results: Any) -> tuple[str | None, list[dict[str, Any]]]:
    manual_results: list[ManualSearchResult] = []
    response_mapping: list[dict[str, Any]] = []
    if not isinstance(results, list):
        return None, response_mapping

    for fallback_index, result in enumerate(results, start=1):
        if not isinstance(result, dict):
            continue
        index = result.get("index") if isinstance(result.get("index"), int) else fallback_index
        title = _string_value(result.get("title")) or _string_value(result.get("label"))
        if not title:
            continue
        manual_results.append(
            ManualSearchResult(
                index=index,
                title=title,
                quality=_string_value(result.get("quality")) or "",
                seeders=result.get("seeders") if isinstance(result.get("seeders"), int) else None,
                size=result.get("size") if isinstance(result.get("size"), int) else None,
                download_link=_string_value(result.get("download_link")) or "",
                label=_string_value(result.get("label")),
            )
        )
        response_mapping.append(_agent_clarify_mapping(index))
        if len(manual_results) >= _AGENT_CLARIFY_MAX_ROWS:
            break

    if not manual_results:
        return None, response_mapping
    return render_choice_table(manual_results), response_mapping


def _title_clarify_display(candidates: Any) -> tuple[str | None, list[dict[str, Any]]]:
    movie_candidates: list[MovieCandidate] = []
    response_mapping: list[dict[str, Any]] = []
    if not isinstance(candidates, list):
        return None, response_mapping

    for fallback_index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        index = candidate.get("index") if isinstance(candidate.get("index"), int) else fallback_index
        label = _string_value(candidate.get("label")) or _string_value(candidate.get("title"))
        title = _string_value(candidate.get("title")) or label
        if not label:
            continue
        movie_candidates.append(
            MovieCandidate(
                index=index,
                title=title or label,
                year=candidate.get("year") if isinstance(candidate.get("year"), int) else None,
                imdb_id=_string_value(candidate.get("imdb_id")) or "",
                label=label,
            )
        )
        response_mapping.append(_agent_clarify_mapping(index))
        if len(movie_candidates) >= _AGENT_CLARIFY_MAX_ROWS:
            break

    if not movie_candidates:
        return None, response_mapping
    return render_title_choice_table(movie_candidates), response_mapping


def _agent_clarify_mapping(index: int) -> dict[str, Any]:
    response = str(index)
    return {"choice": response, "response": response, "index": index}


def _completion_metadata_from_payload(
    payload: dict[str, Any],
    *,
    completion_followup_message: str | None = None,
) -> dict[str, str]:
    download_status = payload.get("download_status") or {}
    content_path = _string_value(download_status.get("content_path")) if _download_status_complete(download_status) else None
    return _completion_metadata(
        imdb_id=_string_value(payload.get("imdb_id")),
        media_type=_string_value(payload.get("media_type")),
        content_path=content_path,
        completion_followup_message=completion_followup_message,
    )


def _completion_metadata(
    *,
    imdb_id: str | None = None,
    media_type: str | None = None,
    content_path: str | None = None,
    completion_followup_message: str | None = None,
) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if imdb_id:
        metadata["imdb_id"] = imdb_id
    if media_type:
        metadata["media_type"] = media_type
    if content_path:
        metadata["content_path"] = content_path
    followup_message = _string_value(completion_followup_message)
    if followup_message:
        metadata["completion_followup_message"] = followup_message
    return metadata


def _download_status_complete(status: dict[str, Any]) -> bool:
    try:
        progress = float(status.get("progress", 0.0))
    except (TypeError, ValueError):
        progress = 0.0
    return progress >= 1.0 or str(status.get("state", "")) in {"uploading", "stalledUP", "pausedUP", "forcedUP", "queuedUP"}


def _start_notifier_if_pending(notifier: DownloadCompletionNotifier) -> None:
    if notifier.store.pending_watches():
        notifier.start()


def _suppress_progress_watch_message(payload: dict[str, Any]) -> None:
    if _string_value(payload.get("message")):
        payload["message"] = ""


def _resolve_notification_target(
    notification_target: str | None,
    requester_id: str | None,
) -> str | None:
    return resolve_notification_target(notification_target, requester_id)


async def run_mcp_server() -> None:
    server = create_mcp_server()
    await server.run_stdio_async()


def main() -> None:
    try:
        asyncio.run(run_mcp_server())
    except KeyboardInterrupt:
        logger.info("qBitlarr MCP server stopped")


if __name__ == "__main__":
    main()
