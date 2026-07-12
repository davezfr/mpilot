from __future__ import annotations

from typing import Any

from mpilot.acquisition.domain.choice_table import (
    render_choice_table,
    render_title_choice_table,
    render_unverified_choice_table,
)
from mpilot.acquisition.models import ManualSearchResult, MovieCandidate
from mpilot.core.targets import resolve_notification_target
from mpilot.mcp.acquisition_notifications import DownloadCompletionNotifier


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
        display_table, response_mapping = _release_clarify_display(
            payload.get("results"),
            results_verified_by_imdb_id=payload.get("results_verified_by_imdb_id") is True,
        )
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


def _release_clarify_display(
    results: Any,
    *,
    results_verified_by_imdb_id: bool,
) -> tuple[str | None, list[dict[str, Any]]]:
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
                indexer=_string_value(result.get("indexer")),
                label=_string_value(result.get("label")),
            )
        )
        response_mapping.append(_agent_clarify_mapping(index))
        if len(manual_results) >= _AGENT_CLARIFY_MAX_ROWS:
            break

    if not manual_results:
        return None, response_mapping
    display_table = (
        render_choice_table(manual_results)
        if results_verified_by_imdb_id
        else render_unverified_choice_table(manual_results)
    )
    return display_table, response_mapping


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
