from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from . import MediaWorkflowRuntime, QBitlarrHashNotTrackedError, RuntimeStoreError


BabelarrJobCreateVideo = Callable[..., Dict[str, Any]]
BabelarrJobStart = Callable[..., Dict[str, Any]]


def dispatch_qbitlarr_completion(
    runtime: MediaWorkflowRuntime,
    *,
    info_hash: str,
    content_path: str,
    limit: Optional[int] = None,
    job_store_dir: Optional[str] = None,
    notification_target: Optional[str] = None,
    requester_id: Optional[str] = None,
    notification_language: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    openai_api_key: Optional[str] = None,
    allow_low_confidence_subtitle: bool = False,
    allow_provider_fallback_language: bool = False,
    opensubtitles_api_key: Optional[str] = None,
    opensubtitles_user_agent: Optional[str] = None,
    opensubtitles_username: Optional[str] = None,
    opensubtitles_password: Optional[str] = None,
    opensubtitles_token: Optional[str] = None,
    subdl_api_key: Optional[str] = None,
    babelarr_job_create_video: Optional[BabelarrJobCreateVideo] = None,
    babelarr_job_start: Optional[BabelarrJobStart] = None,
    mst_job_create_video: Optional[BabelarrJobCreateVideo] = None,
    mst_job_start: Optional[BabelarrJobStart] = None,
) -> Dict[str, Any]:
    """Record qBitlarr completion and dispatch ready Babelarr subtitle jobs."""
    if not str(content_path or "").strip():
        raise RuntimeStoreError("content_path is required for qBitlarr completion dispatch")

    untracked_completion: Optional[Dict[str, Any]] = None
    try:
        runtime.mark_qbitlarr_download_complete(info_hash=info_hash, content_path=content_path)
    except QBitlarrHashNotTrackedError as error:
        # The completion hook can fire for a torrent we never tracked — e.g. the user
        # requested subtitles via a local-video intent for a file that qBitlarr was
        # downloading independently. Surface it in the summary but still let any ready
        # Babelarr actions (from local-video or other workflows) dispatch.
        untracked_completion = {
            "info_hash": error.info_hash,
            "content_path": content_path,
            "reason": "no tracked download workflow for this info_hash",
        }

    summary = dispatch_ready_babelarr_actions(
        runtime,
        limit=limit,
        job_store_dir=job_store_dir,
        notification_target=notification_target,
        requester_id=requester_id,
        notification_language=notification_language,
        backend=backend,
        model=model,
        openai_base_url=openai_base_url,
        openai_api_key=openai_api_key,
        allow_low_confidence_subtitle=allow_low_confidence_subtitle,
        allow_provider_fallback_language=allow_provider_fallback_language,
        opensubtitles_api_key=opensubtitles_api_key,
        opensubtitles_user_agent=opensubtitles_user_agent,
        opensubtitles_username=opensubtitles_username,
        opensubtitles_password=opensubtitles_password,
        opensubtitles_token=opensubtitles_token,
        subdl_api_key=subdl_api_key,
        babelarr_job_create_video=babelarr_job_create_video or mst_job_create_video,
        babelarr_job_start=babelarr_job_start or mst_job_start,
    )
    if untracked_completion is not None:
        summary["untracked_completion"] = untracked_completion
    return summary


def dispatch_ready_babelarr_actions(
    runtime: MediaWorkflowRuntime,
    *,
    workflow_id: Optional[str] = None,
    limit: Optional[int] = None,
    job_store_dir: Optional[str] = None,
    notification_target: Optional[str] = None,
    requester_id: Optional[str] = None,
    notification_language: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    openai_api_key: Optional[str] = None,
    allow_low_confidence_subtitle: bool = False,
    allow_provider_fallback_language: bool = False,
    opensubtitles_api_key: Optional[str] = None,
    opensubtitles_user_agent: Optional[str] = None,
    opensubtitles_username: Optional[str] = None,
    opensubtitles_password: Optional[str] = None,
    opensubtitles_token: Optional[str] = None,
    subdl_api_key: Optional[str] = None,
    babelarr_job_create_video: Optional[BabelarrJobCreateVideo] = None,
    babelarr_job_start: Optional[BabelarrJobStart] = None,
) -> Dict[str, Any]:
    create_video = babelarr_job_create_video or _default_babelarr_job_create_video
    start_job = babelarr_job_start or _default_babelarr_job_start
    reconciled = reconcile_terminal_babelarr_jobs(runtime, job_store_dir=job_store_dir)
    actions = runtime.claim_ready_babelarr_job_create_video_actions(limit=limit, workflow_id=workflow_id)
    dispatches = []
    errors = []

    for action in actions:
        workflow_id_value = _required_string(action.get("workflow_id"), "workflow_id")
        task_id = _required_string(action.get("task_id"), "task_id")
        arguments = _action_arguments(action)
        create_kwargs = {
            "imdb_id": arguments.get("imdb_id"),
            "title": arguments.get("title"),
            "media_type": arguments.get("media_type"),
            "source_language": arguments.get("source_language"),
            "target_language": arguments.get("target_language"),
            "output_mode": arguments.get("output_mode"),
            "backend": backend,
            "model": model,
            "job_store_dir": job_store_dir,
            "allow_low_confidence_subtitle": allow_low_confidence_subtitle,
            "allow_provider_fallback_language": allow_provider_fallback_language,
        }
        if arguments.get("season") is not None:
            create_kwargs["season"] = arguments.get("season")
        if arguments.get("episode") is not None:
            create_kwargs["episode"] = arguments.get("episode")
        try:
            create_payload = create_video(
                _required_string(arguments.get("video_path"), "video_path"),
                **create_kwargs,
            )
            babelarr_job_id = _job_id_from_create_payload(create_payload)
            runtime.record_babelarr_job_created(
                workflow_id=workflow_id_value,
                task_id=task_id,
                babelarr_job_id=babelarr_job_id,
            )
            action_requester_id = _string_value(action.get("requester_id"))
            notification_requester_id = action_requester_id or requester_id
            action_notification_target = _string_value(action.get("notification_target"))
            if not action_notification_target:
                if notification_target and (requester_id is None or action_requester_id == requester_id):
                    action_notification_target = notification_target
                else:
                    action_notification_target = action_requester_id or requester_id
            start_payload = start_job(
                babelarr_job_id,
                job_store_dir=_string_value(create_payload.get("job_store")) or job_store_dir,
                openai_base_url=openai_base_url,
                openai_api_key=openai_api_key,
                allow_low_confidence_subtitle=allow_low_confidence_subtitle,
                allow_provider_fallback_language=allow_provider_fallback_language,
                opensubtitles_api_key=opensubtitles_api_key,
                opensubtitles_user_agent=opensubtitles_user_agent,
                opensubtitles_username=opensubtitles_username,
                opensubtitles_password=opensubtitles_password,
                opensubtitles_token=opensubtitles_token,
                subdl_api_key=subdl_api_key,
                notification_target=action_notification_target,
                requester_id=notification_requester_id,
                title=_string_value(arguments.get("title")),
                notification_language=notification_language or _notification_language(action, arguments),
                runtime_store_dir=str(runtime.root),
                runtime_workflow_id=workflow_id_value,
                runtime_task_id=task_id,
            )
            runtime.record_babelarr_job_status(
                workflow_id=workflow_id_value,
                task_id=task_id,
                status=_runtime_status_from_start_payload(start_payload),
                status_detail=_status_detail_from_payload(start_payload),
                error=_error_from_payload(start_payload),
            )
            dispatches.append(
                {
                    "action": action,
                    "babelarr_job": create_payload,
                    "babelarr_start": start_payload,
                }
            )
        except Exception as error:
            runtime.record_babelarr_job_status(
                workflow_id=workflow_id_value,
                task_id=task_id,
                status="failed",
                error={"type": type(error).__name__, "message": str(error)},
            )
            errors.append(
                {
                    "action": action,
                    "error": {"type": type(error).__name__, "message": str(error)},
                }
            )
            continue

    return {
        "status": "partial_failure" if errors else "ok",
        "actions_claimed": len(actions),
        "dispatches": dispatches,
        "errors": errors,
        "reconciled": reconciled,
    }


def dispatch_ready_mst_actions(
    runtime: MediaWorkflowRuntime,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Legacy alias for dispatch_ready_babelarr_actions."""
    if "mst_job_create_video" in kwargs:
        kwargs["babelarr_job_create_video"] = kwargs.pop("mst_job_create_video")
    if "mst_job_start" in kwargs:
        kwargs["babelarr_job_start"] = kwargs.pop("mst_job_start")
    return dispatch_ready_babelarr_actions(runtime, **kwargs)


def reconcile_terminal_babelarr_jobs(
    runtime: MediaWorkflowRuntime,
    *,
    job_store_dir: Optional[str] = None,
) -> list[Dict[str, Any]]:
    if not _string_value(job_store_dir):
        from mpilot.subtitles.jobs import default_job_store_dir

        job_store_dir = str(default_job_store_dir())
    return _reconcile_terminal_mst_jobs(runtime, job_store_dir)


def _default_babelarr_job_create_video(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    from mpilot.subtitles.mcp_server import _job_create_video_with_store

    return _job_create_video_with_store(*args, **kwargs)


def _default_babelarr_job_start(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    from mpilot.subtitles.mcp_server import _job_start_with_overrides

    return _job_start_with_overrides(*args, **kwargs)


STALE_DISPATCHING_AFTER_SECONDS = 900


def _reconcile_terminal_mst_jobs(
    runtime: MediaWorkflowRuntime,
    job_store_dir: Optional[str],
) -> list[Dict[str, Any]]:
    if not _string_value(job_store_dir):
        return []

    from mpilot.subtitles.jobs import JobStore

    store = JobStore(Path(_required_string(job_store_dir, "job_store_dir")))
    reconciled = []
    for workflow in runtime.list_workflows():
        workflow_id = _string_value(workflow.get("workflow_id"))
        if not workflow_id:
            continue
        for task in workflow.get("tasks", []):
            if not isinstance(task, Mapping):
                continue
            if task.get("task_type") != "translate_subtitle":
                continue
            if task.get("status") not in {"dispatching", "queued", "running"}:
                continue
            task_id = _string_value(task.get("task_id"))
            if not task_id:
                continue
            babelarr = task.get("babelarr")
            job_id = _string_value(babelarr.get("job_id")) if isinstance(babelarr, Mapping) else None
            if not job_id:
                # A task claimed for dispatch but never tied to an Babelarr job
                # (e.g. the dispatcher crashed mid-dispatch) would otherwise
                # hold the single-active-task slot forever.
                if task.get("status") == "dispatching" and _is_stale(task.get("updated_at")):
                    runtime.record_babelarr_job_status(
                        workflow_id=workflow_id,
                        task_id=task_id,
                        status="failed",
                        error={
                            "type": "StaleDispatchError",
                            "message": "task stuck in dispatching without an Babelarr job; marked failed by reconcile",
                        },
                    )
                    reconciled.append(
                        {
                            "workflow_id": workflow_id,
                            "task_id": task_id,
                            "babelarr_job_id": None,
                            "status": "failed",
                        }
                    )
                continue
            try:
                job = store.get(job_id)
            except Exception:
                continue
            status = str(job.get("status") or "")
            if status not in {"succeeded", "failed", "needs_confirmation"}:
                continue
            runtime.record_babelarr_job_status(
                workflow_id=workflow_id,
                task_id=task_id,
                status=status,
                status_detail=_job_status_detail(job),
                result=job.get("result") if isinstance(job.get("result"), Mapping) else None,
                error=job.get("last_error") if isinstance(job.get("last_error"), Mapping) else None,
            )
            reconciled.append(
                {
                    "workflow_id": workflow_id,
                    "task_id": task_id,
                    "babelarr_job_id": job_id,
                    "status": status,
                }
            )
    return reconciled


def _action_arguments(action: Mapping[str, Any]) -> Mapping[str, Any]:
    arguments = action.get("arguments")
    if not isinstance(arguments, Mapping):
        raise RuntimeStoreError("Runtime action has no arguments")
    return arguments


def _job_id_from_create_payload(payload: Mapping[str, Any]) -> str:
    job = payload.get("job")
    if not isinstance(job, Mapping):
        raise RuntimeStoreError("Babelarr job_create_video did not return a job object")
    return _required_string(job.get("job_id"), "babelarr_job_id")


def _runtime_status_from_start_payload(payload: Mapping[str, Any]) -> str:
    status = payload.get("status")
    if status in {"started", "already_running"}:
        return "running"
    if status == "needs_confirmation":
        return "needs_confirmation"
    if status == "succeeded":
        return "succeeded"
    if status == "failed":
        return "failed"
    if status == "error":
        return "failed"
    return "queued"


def _status_detail_from_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    status_detail = payload.get("status_detail")
    if isinstance(status_detail, Mapping):
        return dict(status_detail)
    return {"stage": "babelarr_job_start", "status": str(payload.get("status") or "unknown")}


def _error_from_payload(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    error = payload.get("error")
    if isinstance(error, Mapping):
        return dict(error)
    return None


def _is_stale(updated_at: Any, *, now: Optional[datetime] = None) -> bool:
    if not isinstance(updated_at, str) or not updated_at.strip():
        return True
    try:
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return (current - updated).total_seconds() >= STALE_DISPATCHING_AFTER_SECONDS


def _job_status_detail(job: Mapping[str, Any]) -> Dict[str, Any]:
    progress = job.get("progress")
    if isinstance(progress, Mapping):
        return dict(progress)
    return {"stage": str(job.get("status") or "unknown"), "status": str(job.get("status") or "unknown")}


def _notification_language(action: Mapping[str, Any], arguments: Mapping[str, Any]) -> Optional[str]:
    notification_language = _string_value(action.get("notification_language")) or _string_value(
        arguments.get("notification_language")
    )
    if notification_language:
        return notification_language
    target_language = _string_value(arguments.get("target_language"))
    if target_language in {"zh", "fr", "en"}:
        return target_language
    return None


def _required_string(value: Any, name: str) -> str:
    text = _string_value(value)
    if not text:
        raise RuntimeStoreError("%s is required" % name)
    return text


def _string_value(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
