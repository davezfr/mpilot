from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

from . import MediaWorkflowRuntime, RuntimeStoreError
from .cli import _mapped_content_path, default_runtime_store_dir
from .dispatcher import reconcile_terminal_babelarr_jobs


SERVER_NAME = "runtime"
SERVER_INSTRUCTIONS = (
    "Coordinate cross-MCP media workflows. Use this server to record acquisition "
    "download state, attach subtitle intent, record combined download plus "
    "subtitle requests, record local/Plex video subtitle requests, claim ready "
    "MPilot subtitle actions, inspect the global subtitle queue, record downstream subtitle job "
    "IDs and status, and show unified workflow state. "
    "Do not use this server for media search/download or subtitle translation; "
    "use MPilot acquisition and subtitle tools for those operations."
)


def record_qbitlarr_download(
    *,
    requester_id: str,
    info_hash: str,
    title: Optional[str] = None,
    imdb_id: Optional[str] = None,
    media_type: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    progress: float = 0.0,
    content_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update a qBitlarr download task in the Runtime workflow store."""
    return _runtime().record_qbitlarr_download(
        requester_id=requester_id,
        info_hash=info_hash,
        title=title,
        imdb_id=imdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        progress=progress,
        content_path=_map_path_from_env(content_path),
    )


def record_acquisition_download(
    *,
    requester_id: str,
    info_hash: str,
    title: Optional[str] = None,
    imdb_id: Optional[str] = None,
    media_type: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    progress: float = 0.0,
    content_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update an acquisition download task in the Runtime workflow store."""
    return record_qbitlarr_download(
        requester_id=requester_id,
        info_hash=info_hash,
        title=title,
        imdb_id=imdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        progress=progress,
        content_path=content_path,
    )


def record_qbitlarr_download_with_subtitle_intent(
    *,
    requester_id: str,
    info_hash: str,
    source_language: str,
    target_language: str,
    output_mode: str,
    notification_language: Optional[str] = None,
    notification_target: Optional[str] = None,
    title: Optional[str] = None,
    imdb_id: Optional[str] = None,
    media_type: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    progress: float = 0.0,
    content_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update a qBitlarr download and attach subtitle intent in one call."""
    return _runtime().record_qbitlarr_download_with_subtitle_intent(
        requester_id=requester_id,
        info_hash=info_hash,
        title=title,
        imdb_id=imdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        progress=progress,
        content_path=_map_path_from_env(content_path),
        source_language=source_language,
        target_language=target_language,
        output_mode=_validated_output_mode(output_mode),
        notification_language=notification_language,
        notification_target=notification_target,
    )


def record_acquisition_download_with_subtitle_intent(
    *,
    requester_id: str,
    info_hash: str,
    source_language: str,
    target_language: str,
    output_mode: str,
    notification_language: Optional[str] = None,
    notification_target: Optional[str] = None,
    title: Optional[str] = None,
    imdb_id: Optional[str] = None,
    media_type: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    progress: float = 0.0,
    content_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update an acquisition download and attach subtitle intent in one call."""
    return record_qbitlarr_download_with_subtitle_intent(
        requester_id=requester_id,
        info_hash=info_hash,
        source_language=source_language,
        target_language=target_language,
        output_mode=output_mode,
        notification_language=notification_language,
        notification_target=notification_target,
        title=title,
        imdb_id=imdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        progress=progress,
        content_path=content_path,
    )


def attach_subtitle_intent(
    *,
    requester_id: str,
    source_language: str,
    target_language: str,
    output_mode: str,
    notification_language: Optional[str] = None,
    notification_target: Optional[str] = None,
) -> Dict[str, Any]:
    """Attach subtitle intent to the requester's single active download."""
    return _runtime().attach_subtitle_intent_to_current_download(
        requester_id=requester_id,
        source_language=source_language,
        target_language=target_language,
        output_mode=_validated_output_mode(output_mode),
        notification_language=notification_language,
        notification_target=notification_target,
    )


def record_local_video_subtitle_intent(
    *,
    requester_id: str,
    video_path: str,
    source_language: str,
    target_language: str,
    output_mode: str,
    notification_language: Optional[str] = None,
    notification_target: Optional[str] = None,
    title: Optional[str] = None,
    imdb_id: Optional[str] = None,
    media_type: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> Dict[str, Any]:
    """Record a ready subtitle task for an already local or Plex-resolved video file."""
    return _runtime().record_local_video_subtitle_intent(
        requester_id=requester_id,
        video_path=_map_path_from_env(video_path) or video_path,
        title=title,
        imdb_id=imdb_id,
        media_type=media_type,
        season=season,
        episode=episode,
        source_language=source_language,
        target_language=target_language,
        output_mode=_validated_output_mode(output_mode),
        notification_language=notification_language,
        notification_target=notification_target,
    )


def claim_ready_babelarr_job_create_video_actions(
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Claim ready Babelarr direct-video actions so they are not dispatched twice."""
    runtime = _runtime()
    reconcile_terminal_babelarr_jobs(runtime, job_store_dir=_job_store_dir())
    return runtime.claim_ready_babelarr_job_create_video_actions(limit=limit)


def claim_ready_subtitle_job_create_video_actions(
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Claim ready MPilot direct-video subtitle actions so they are not dispatched twice."""
    actions = claim_ready_babelarr_job_create_video_actions(
        limit=limit,
    )
    return [_mpilot_subtitle_action(action) for action in actions]


def record_babelarr_job_created(
    *,
    workflow_id: str,
    task_id: str,
    babelarr_job_id: str,
) -> Dict[str, Any]:
    """Record the Babelarr job ID returned after creating a direct-video subtitle job."""
    return _runtime().record_babelarr_job_created(
        workflow_id=workflow_id,
        task_id=task_id,
        babelarr_job_id=babelarr_job_id,
    )


def record_subtitle_job_created(
    *,
    workflow_id: str,
    task_id: str,
    subtitle_job_id: str,
) -> Dict[str, Any]:
    """Record the MPilot subtitle job ID returned after creating a direct-video subtitle job."""
    return record_babelarr_job_created(
        workflow_id=workflow_id,
        task_id=task_id,
        babelarr_job_id=subtitle_job_id,
    )


def record_babelarr_job_status(
    *,
    workflow_id: str,
    task_id: str,
    status: str,
    status_detail: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Mirror Babelarr job status into the Runtime workflow."""
    return _runtime().record_babelarr_job_status(
        workflow_id=workflow_id,
        task_id=task_id,
        status=status,
        status_detail=status_detail,
        result=result,
        error=error,
    )


def record_subtitle_job_status(
    *,
    workflow_id: str,
    task_id: str,
    status: str,
    status_detail: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Mirror MPilot subtitle job status into the Runtime workflow."""
    return record_babelarr_job_status(
        workflow_id=workflow_id,
        task_id=task_id,
        status=status,
        status_detail=status_detail,
        result=result,
        error=error,
    )


def claim_ready_mst_job_create_video_actions(
    *,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Legacy alias for claim_ready_babelarr_job_create_video_actions."""
    return claim_ready_babelarr_job_create_video_actions(
        limit=limit,
    )


def record_mst_job_created(
    *,
    workflow_id: str,
    task_id: str,
    mst_job_id: str,
) -> Dict[str, Any]:
    """Legacy alias for record_babelarr_job_created."""
    return record_babelarr_job_created(
        workflow_id=workflow_id,
        task_id=task_id,
        babelarr_job_id=mst_job_id,
    )


def record_mst_job_status(
    *,
    workflow_id: str,
    task_id: str,
    status: str,
    status_detail: Optional[Dict[str, Any]] = None,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Legacy alias for record_babelarr_job_status."""
    return record_babelarr_job_status(
        workflow_id=workflow_id,
        task_id=task_id,
        status=status,
        status_detail=status_detail,
        result=result,
        error=error,
    )


def queue_status(
    *,
    requester_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return global subtitle queue counts and requester-visible task positions."""
    runtime = _runtime()
    reconcile_terminal_babelarr_jobs(runtime, job_store_dir=_job_store_dir())
    return runtime.queue_status(requester_id=requester_id)


def workflow_show(*, workflow_id: str) -> Dict[str, Any]:
    """Return one Runtime workflow summary."""
    return _runtime().workflow_summary(workflow_id)


def list_workflows() -> List[Dict[str, Any]]:
    """Return Runtime workflows ordered by update time."""
    return _runtime().list_workflows()


def _mpilot_subtitle_action(action: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(action)
    if normalized.get("action") == "babelarr_job_create_video":
        normalized["action"] = "subtitle_job_create_video"
    return normalized


def create_mcp_server():
    from mpilot.core.dotenv import load_project_dotenv

    load_project_dotenv()
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as error:
        if error.name == "mcp":
            raise RuntimeError("Runtime MCP support requires: python3 -m pip install -e '.[mcp]'") from error
        raise

    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    mcp.tool()(record_acquisition_download)
    mcp.tool()(record_acquisition_download_with_subtitle_intent)
    mcp.tool()(attach_subtitle_intent)
    mcp.tool()(record_local_video_subtitle_intent)
    mcp.tool()(claim_ready_subtitle_job_create_video_actions)
    mcp.tool()(record_subtitle_job_created)
    mcp.tool()(record_subtitle_job_status)
    mcp.tool()(queue_status)
    mcp.tool()(workflow_show)
    mcp.tool()(list_workflows)
    return mcp


def main() -> int:
    try:
        create_mcp_server().run()
    except RuntimeError as error:
        print("MPilot runtime MCP: %s" % error, file=sys.stderr)
        return 1
    return 0


def _runtime() -> MediaWorkflowRuntime:
    return MediaWorkflowRuntime(default_runtime_store_dir())


def _job_store_dir() -> Optional[str]:
    return (
        os.environ.get("MPILOT_SUBTITLE_JOB_STORE_DIR")
        or os.environ.get("BABELARR_JOB_STORE_DIR")
        or os.environ.get("MST_JOB_STORE_DIR")
    )


def _map_path_from_env(value: Optional[str]) -> Optional[str]:
    """Apply configured Runtime path mapping to a container-side path.

    Lets MCP callers (e.g. bots that don't know about the qBitlarr container vs host
    path split) hand in the raw content_path / video_path and have it rewritten before
    it reaches the workflow store.
    """
    return _mapped_content_path(
        value,
        content_path_prefix=(
            os.environ.get("MPILOT_RUNTIME_CONTENT_PATH_PREFIX")
            or os.environ.get("BABELARR_RUNTIME_CONTENT_PATH_PREFIX")
            or os.environ.get("MWR_CONTENT_PATH_PREFIX")
        ),
        local_content_path_prefix=(
            os.environ.get("MPILOT_RUNTIME_LOCAL_CONTENT_PATH_PREFIX")
            or os.environ.get("BABELARR_RUNTIME_LOCAL_CONTENT_PATH_PREFIX")
            or os.environ.get("MWR_LOCAL_CONTENT_PATH_PREFIX")
        ),
    )


def _validated_output_mode(output_mode: str) -> str:
    value = str(output_mode or "").strip()
    allowed = {"single-srt", "bilingual-ass"}
    if value not in allowed:
        raise RuntimeStoreError("output_mode must be one of: bilingual-ass, single-srt")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
