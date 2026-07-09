from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional, Sequence

from . import cli as cli_module
from .provider_policy import LowConfidenceSubtitleCandidatesError


SERVER_NAME = "mpilot-subtitles"
SERVER_INSTRUCTIONS = (
    "Resolve Plex media by ID or title, plan subtitle availability, create and "
    "run persistent subtitle translation jobs, and write Plex-compatible "
    "sidecar subtitles. "
    "Use qBitlarr or another download tool for media acquisition; use this "
    "server only for subtitle planning, subtitle translation jobs, and job "
    "recovery. For Telegram or other chat workflows, prefer job_create followed "
    "by job_start, then use job_show for progress checks. job_run is synchronous "
    "and can exceed chat tool timeouts. Use job_confirm_low_confidence only "
    "after the user accepts a low-confidence subtitle timing-risk proposal. "
    "When a chat user expects running status and completion notices, pass "
    "notification_target or a requester_id such as telegram:<user id> to "
    "job_start."
)

def run_cli_summary(argv: Sequence[str]) -> Dict[str, Any]:
    command = argv[0] if argv else None
    try:
        parser = cli_module.build_parser()
        args = parser.parse_args(list(argv))
        return cli_module.summary_from_args(args)
    except LowConfidenceSubtitleCandidatesError as error:
        return {
            "status": "needs_confirmation",
            "proposal": error.to_dict(),
            **cli_module.cli_error_summary(error, command),
        }
    except SystemExit as error:
        return {
            "status": "error",
            "command": command,
            "error": {
                "type": "ArgumentError",
                "message": "invalid arguments",
                "exit_code": error.code,
            },
        }
    except Exception as error:
        return {
            "status": "error",
            **cli_module.cli_error_summary(error, command),
        }


def plex_search(
    *,
    query: str,
    year: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    limit: int = 10,
    plex_base_url: Optional[str] = None,
    plex_token: Optional[str] = None,
    plex_path_prefix: Optional[str] = None,
    local_path_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """Search Plex/local library by title and return local media candidates.

    Use this for local-first subtitle routing when the user names a movie or TV
    episode but does not provide an IMDb ID or Plex ratingKey. The tool does not
    create jobs, download subtitles, or write sidecars.
    """
    argv = ["plex-search"]
    _append_value(argv, "--query", query)
    _append_value(argv, "--year", year)
    _append_value(argv, "--season", season)
    _append_value(argv, "--episode", episode)
    _append_value(argv, "--limit", limit)
    _append_plex_connection(
        argv,
        plex_base_url=plex_base_url,
        plex_token=plex_token,
        plex_path_prefix=plex_path_prefix,
        local_path_prefix=local_path_prefix,
    )
    return run_cli_summary(argv)


def subtitle_plan(
    *,
    imdb: Optional[str] = None,
    rating_key: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    target_language: str,
    preferred_source_language: str = "en",
    plex_base_url: Optional[str] = None,
    plex_token: Optional[str] = None,
    plex_path_prefix: Optional[str] = None,
    local_path_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    """Plan local subtitle availability for one Plex movie or episode.

    Use this before creating a translation job when the user is asking whether
    a target-language subtitle already exists or whether translation is needed.
    Pass exactly one of imdb or rating_key. Plex credentials can be supplied as
    arguments or through PLEX_BASE_URL and PLEX_TOKEN.
    """
    argv = ["subtitle-plan"]
    _append_resource(argv, imdb=imdb, rating_key=rating_key, season=season, episode=episode)
    _append_plex_connection(
        argv,
        plex_base_url=plex_base_url,
        plex_token=plex_token,
        plex_path_prefix=plex_path_prefix,
        local_path_prefix=local_path_prefix,
    )
    _append_value(argv, "--target-language", target_language)
    _append_value(argv, "--preferred-source-language", preferred_source_language)
    return run_cli_summary(argv)


def job_create(
    *,
    imdb: Optional[str] = None,
    rating_key: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    source_language: Optional[str] = None,
    target_language: Optional[str] = None,
    output_mode: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    plex_base_url: Optional[str] = None,
    plex_path_prefix: Optional[str] = None,
    local_path_prefix: Optional[str] = None,
    output: Optional[str] = None,
    force: bool = False,
    work_dir: Optional[str] = None,
    keep_work_dir: bool = False,
    write_back: bool = False,
    refresh_plex: bool = False,
    no_online_subtitle_fallback: bool = False,
    allow_low_confidence_subtitle: bool = False,
    allow_provider_fallback_language: bool = False,
    assume_unlabeled_stream_language: bool = False,
    subtitle_provider: Optional[str] = None,
    provider_search_limit: Optional[int] = None,
    download_provider_priority: Optional[str] = None,
    primary_script: Optional[str] = None,
    secondary_script: Optional[str] = None,
    ass_height: Optional[int] = None,
    job_store_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a persistent translate-plex job.

    This records the user's explicit Plex resource and subtitle intent in the
    local job store. It does not run translation and does not store Plex tokens,
    provider credentials, or model API keys. Pass those later to job_run or via
    the worker environment.
    """
    argv = ["job-create"]
    _append_value(argv, "--job-store-dir", job_store_dir)
    _append_resource(argv, imdb=imdb, rating_key=rating_key, season=season, episode=episode)
    _append_value(argv, "--plex-base-url", plex_base_url)
    _append_value(argv, "--plex-path-prefix", plex_path_prefix)
    _append_value(argv, "--local-path-prefix", local_path_prefix)
    _append_value(argv, "--source-language", source_language)
    _append_value(argv, "--target-language", target_language)
    _append_value(argv, "--backend", backend)
    _append_value(argv, "--model", model)
    _append_value(argv, "--output-mode", output_mode)
    _append_value(argv, "--output", output)
    _append_flag(argv, "--force", force)
    _append_value(argv, "--work-dir", work_dir)
    _append_flag(argv, "--keep-work-dir", keep_work_dir)
    _append_flag(argv, "--write-back", write_back)
    _append_flag(argv, "--refresh-plex", refresh_plex)
    _append_flag(argv, "--no-online-subtitle-fallback", no_online_subtitle_fallback)
    _append_flag(argv, "--allow-low-confidence-subtitle", allow_low_confidence_subtitle)
    _append_flag(argv, "--allow-provider-fallback-language", allow_provider_fallback_language)
    _append_flag(argv, "--assume-unlabeled-stream-language", assume_unlabeled_stream_language)
    _append_value(argv, "--subtitle-provider", subtitle_provider)
    _append_value(argv, "--provider-search-limit", provider_search_limit)
    _append_value(argv, "--download-provider-priority", download_provider_priority)
    _append_value(argv, "--primary-script", primary_script)
    _append_value(argv, "--secondary-script", secondary_script)
    _append_value(argv, "--ass-height", ass_height)
    return run_cli_summary(argv)


def job_show(job_id: str, *, job_store_dir: Optional[str] = None) -> Dict[str, Any]:
    """Return one persistent job record by ID."""
    argv = ["job-show"]
    _append_value(argv, "--job-store-dir", job_store_dir)
    argv.append(job_id)
    return run_cli_summary(argv)


def job_run(
    job_id: str,
    *,
    job_store_dir: Optional[str] = None,
    plex_base_url: Optional[str] = None,
    plex_token: Optional[str] = None,
    plex_path_prefix: Optional[str] = None,
    local_path_prefix: Optional[str] = None,
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
) -> Dict[str, Any]:
    """Run or retry one persistent subtitle translation job.

    Use this after job_create. Runtime-only secrets such as PLEX_TOKEN,
    OpenSubtitles credentials, SubDL API key, and OpenAI-compatible settings can
    be supplied as arguments or through the worker environment. This call is
    synchronous and may exceed chat/MCP timeouts; use job_start for Telegram or
    other user-facing flows that need immediate feedback.
    """
    argv = _job_runtime_argv("job-run")
    _append_job_runtime(
        argv,
        job_store_dir=job_store_dir,
        plex_base_url=plex_base_url,
        plex_token=plex_token,
        plex_path_prefix=plex_path_prefix,
        local_path_prefix=local_path_prefix,
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
    )
    argv.append(job_id)
    return run_cli_summary(argv)


def job_start(
    job_id: str,
    *,
    job_store_dir: Optional[str] = None,
    plex_base_url: Optional[str] = None,
    plex_token: Optional[str] = None,
    plex_path_prefix: Optional[str] = None,
    local_path_prefix: Optional[str] = None,
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
    notification_target: Optional[str] = None,
    requester_id: Optional[str] = None,
    title: Optional[str] = None,
    notification_language: Optional[str] = None,
    runtime_store_dir: Optional[str] = None,
    runtime_workflow_id: Optional[str] = None,
    runtime_task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Start one persistent subtitle translation job in the background.

    Use this after job_create for Telegram or other chat workflows. It returns
    immediately with the job id and worker pid so the Bot can tell the user that
    processing has started, then call job_show later for status. Runtime-only
    secrets are passed to the worker environment, not stored in the job record.
    Pass notification_target or requester_id as a Hermes send target such as
    "telegram:<chat-id>" to send running status updates plus one terminal
    status notice.
    """
    argv = _job_runtime_argv("job-start")
    _append_job_runtime(
        argv,
        job_store_dir=job_store_dir,
        plex_base_url=plex_base_url,
        plex_token=plex_token,
        plex_path_prefix=plex_path_prefix,
        local_path_prefix=local_path_prefix,
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
    )
    _append_value(argv, "--notification-target", notification_target)
    _append_value(argv, "--requester-id", requester_id)
    _append_value(argv, "--notification-title", title)
    _append_value(argv, "--notification-language", notification_language)
    _append_value(argv, "--runtime-store-dir", runtime_store_dir)
    _append_value(argv, "--runtime-workflow-id", runtime_workflow_id)
    _append_value(argv, "--runtime-task-id", runtime_task_id)
    argv.append(job_id)
    return run_cli_summary(argv)


def job_resume(
    *,
    stale_after_seconds: int = 3600,
    limit: int = 10,
    job_store_dir: Optional[str] = None,
    plex_base_url: Optional[str] = None,
    plex_token: Optional[str] = None,
    plex_path_prefix: Optional[str] = None,
    local_path_prefix: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    openai_api_key: Optional[str] = None,
    opensubtitles_api_key: Optional[str] = None,
    opensubtitles_user_agent: Optional[str] = None,
    opensubtitles_username: Optional[str] = None,
    opensubtitles_password: Optional[str] = None,
    opensubtitles_token: Optional[str] = None,
    subdl_api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Run recoverable queued, failed, or stale-running jobs.

    This is intended for worker recovery after a restart. It deliberately does
    not bulk-confirm low-confidence subtitle matches; use
    job_confirm_low_confidence for the specific job after user approval.
    """
    argv = ["job-resume"]
    _append_job_runtime(
        argv,
        job_store_dir=job_store_dir,
        plex_base_url=plex_base_url,
        plex_token=plex_token,
        plex_path_prefix=plex_path_prefix,
        local_path_prefix=local_path_prefix,
        openai_base_url=openai_base_url,
        openai_api_key=openai_api_key,
        allow_low_confidence_subtitle=False,
        opensubtitles_api_key=opensubtitles_api_key,
        opensubtitles_user_agent=opensubtitles_user_agent,
        opensubtitles_username=opensubtitles_username,
        opensubtitles_password=opensubtitles_password,
        opensubtitles_token=opensubtitles_token,
        subdl_api_key=subdl_api_key,
    )
    _append_value(argv, "--stale-after-seconds", stale_after_seconds)
    _append_value(argv, "--limit", limit)
    return run_cli_summary(argv)


def job_confirm_low_confidence(
    job_id: str,
    *,
    job_store_dir: Optional[str] = None,
    plex_base_url: Optional[str] = None,
    plex_token: Optional[str] = None,
    plex_path_prefix: Optional[str] = None,
    local_path_prefix: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    openai_api_key: Optional[str] = None,
    opensubtitles_api_key: Optional[str] = None,
    opensubtitles_user_agent: Optional[str] = None,
    opensubtitles_username: Optional[str] = None,
    opensubtitles_password: Optional[str] = None,
    opensubtitles_token: Optional[str] = None,
    subdl_api_key: Optional[str] = None,
    notification_target: Optional[str] = None,
    requester_id: Optional[str] = None,
    title: Optional[str] = None,
    notification_language: Optional[str] = None,
) -> Dict[str, Any]:
    """Confirm one low-confidence subtitle match and start that job.

    Call this only after presenting the timing-risk proposal to the user and
    receiving confirmation. This starts work in the background and returns
    immediately so chat integrations can keep showing status with job_show.
    """
    return job_start(
        job_id,
        job_store_dir=job_store_dir,
        plex_base_url=plex_base_url,
        plex_token=plex_token,
        plex_path_prefix=plex_path_prefix,
        local_path_prefix=local_path_prefix,
        openai_base_url=openai_base_url,
        openai_api_key=openai_api_key,
        allow_low_confidence_subtitle=True,
        opensubtitles_api_key=opensubtitles_api_key,
        opensubtitles_user_agent=opensubtitles_user_agent,
        opensubtitles_username=opensubtitles_username,
        opensubtitles_password=opensubtitles_password,
        opensubtitles_token=opensubtitles_token,
        subdl_api_key=subdl_api_key,
        notification_target=notification_target,
        requester_id=requester_id,
        title=title,
        notification_language=notification_language,
    )


def job_confirm_provider_fallback_language(
    job_id: str,
    *,
    job_store_dir: Optional[str] = None,
    plex_base_url: Optional[str] = None,
    plex_token: Optional[str] = None,
    plex_path_prefix: Optional[str] = None,
    local_path_prefix: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    openai_api_key: Optional[str] = None,
    opensubtitles_api_key: Optional[str] = None,
    opensubtitles_user_agent: Optional[str] = None,
    opensubtitles_username: Optional[str] = None,
    opensubtitles_password: Optional[str] = None,
    opensubtitles_token: Optional[str] = None,
    subdl_api_key: Optional[str] = None,
    notification_target: Optional[str] = None,
    requester_id: Optional[str] = None,
    title: Optional[str] = None,
    notification_language: Optional[str] = None,
) -> Dict[str, Any]:
    """Confirm a provider subtitle found in a fallback language and start that job.

    Call this only after presenting the fallback-language proposal to the user and
    receiving confirmation that they accept translating from the fallback language.
    The job needs_confirmation state must include confirmation_reason
    'provider_fallback_language'. This starts work in the background and returns
    immediately so chat integrations can keep showing status with job_show.
    """
    return job_start(
        job_id,
        job_store_dir=job_store_dir,
        plex_base_url=plex_base_url,
        plex_token=plex_token,
        plex_path_prefix=plex_path_prefix,
        local_path_prefix=local_path_prefix,
        openai_base_url=openai_base_url,
        openai_api_key=openai_api_key,
        allow_provider_fallback_language=True,
        opensubtitles_api_key=opensubtitles_api_key,
        opensubtitles_user_agent=opensubtitles_user_agent,
        opensubtitles_username=opensubtitles_username,
        opensubtitles_password=opensubtitles_password,
        opensubtitles_token=opensubtitles_token,
        subdl_api_key=subdl_api_key,
        notification_target=notification_target,
        requester_id=requester_id,
        title=title,
        notification_language=notification_language,
    )


def job_create_video(
    video_path: str,
    *,
    imdb_id: Optional[str] = None,
    title: Optional[str] = None,
    media_type: Optional[str] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    source_language: Optional[str] = None,
    target_language: Optional[str] = None,
    output_mode: Optional[str] = None,
    backend: Optional[str] = None,
    model: Optional[str] = None,
    output: Optional[str] = None,
    force: bool = False,
    work_dir: Optional[str] = None,
    keep_work_dir: bool = False,
    no_online_subtitle_fallback: bool = False,
    allow_low_confidence_subtitle: bool = False,
    allow_provider_fallback_language: bool = False,
    assume_unlabeled_stream_language: bool = False,
    subtitle_provider: Optional[str] = None,
    provider_search_limit: Optional[int] = None,
    download_provider_priority: Optional[str] = None,
    primary_script: Optional[str] = None,
    secondary_script: Optional[str] = None,
    ass_height: Optional[int] = None,
    job_store_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a persistent translate-video job from a direct local file path.

    Use this when you have an absolute path to a video file (e.g. from a qBitlarr
    download notification content_path) and want to translate its subtitles without
    a Plex lookup. Pass imdb_id, title, media_type, season, and episode to improve
    online subtitle provider fallback when no local embedded or sidecar subtitle is found.
    Does not run translation; call job_start or job_run with the returned job_id.
    """
    argv = ["job-create-video", "--video-path", video_path]
    _append_value(argv, "--job-store-dir", job_store_dir)
    _append_value(argv, "--imdb-id", imdb_id)
    _append_value(argv, "--title", title)
    _append_value(argv, "--media-type", media_type)
    _append_value(argv, "--season", season)
    _append_value(argv, "--episode", episode)
    _append_value(argv, "--source-language", source_language)
    _append_value(argv, "--target-language", target_language)
    _append_value(argv, "--backend", backend)
    _append_value(argv, "--model", model)
    _append_value(argv, "--output-mode", output_mode)
    _append_value(argv, "--output", output)
    _append_flag(argv, "--force", force)
    _append_value(argv, "--work-dir", work_dir)
    _append_flag(argv, "--keep-work-dir", keep_work_dir)
    _append_flag(argv, "--no-online-subtitle-fallback", no_online_subtitle_fallback)
    _append_flag(argv, "--allow-low-confidence-subtitle", allow_low_confidence_subtitle)
    _append_flag(argv, "--allow-provider-fallback-language", allow_provider_fallback_language)
    _append_flag(argv, "--assume-unlabeled-stream-language", assume_unlabeled_stream_language)
    _append_value(argv, "--subtitle-provider", subtitle_provider)
    _append_value(argv, "--provider-search-limit", provider_search_limit)
    _append_value(argv, "--download-provider-priority", download_provider_priority)
    _append_value(argv, "--primary-script", primary_script)
    _append_value(argv, "--secondary-script", secondary_script)
    _append_value(argv, "--ass-height", ass_height)
    return run_cli_summary(argv)


def job_prune(
    *,
    retention_days: int = 90,
    dry_run: bool = False,
    job_store_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Delete old succeeded job records from the local job store."""
    argv = ["job-prune"]
    _append_value(argv, "--job-store-dir", job_store_dir)
    _append_value(argv, "--retention-days", retention_days)
    _append_flag(argv, "--dry-run", dry_run)
    return run_cli_summary(argv)


def create_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as error:
        if error.name == "mcp":
            raise RuntimeError("MCP support requires: python3 -m pip install -e '.[mcp]'") from error
        raise

    mcp = FastMCP(SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    mcp.tool()(plex_search)
    mcp.tool()(subtitle_plan)
    mcp.tool()(job_create)
    mcp.tool()(job_create_video)
    mcp.tool()(job_start)
    mcp.tool()(job_show)
    mcp.tool()(job_run)
    mcp.tool()(job_resume)
    mcp.tool()(job_confirm_low_confidence)
    mcp.tool()(job_confirm_provider_fallback_language)
    mcp.tool()(job_prune)
    return mcp


def main() -> int:
    try:
        create_mcp_server().run()
    except RuntimeError as error:
        print("MPilot subtitles MCP: %s" % error, file=sys.stderr)
        return 1
    return 0


def _append_value(argv: List[str], flag: str, value: Any) -> None:
    if value is not None:
        argv.extend([flag, str(value)])


def _append_flag(argv: List[str], flag: str, enabled: bool) -> None:
    if enabled:
        argv.append(flag)


def _append_resource(
    argv: List[str],
    *,
    imdb: Optional[str],
    rating_key: Optional[str],
    season: Optional[int],
    episode: Optional[int],
) -> None:
    _append_value(argv, "--imdb", imdb)
    _append_value(argv, "--rating-key", rating_key)
    _append_value(argv, "--season", season)
    _append_value(argv, "--episode", episode)


def _append_plex_connection(
    argv: List[str],
    *,
    plex_base_url: Optional[str],
    plex_token: Optional[str],
    plex_path_prefix: Optional[str],
    local_path_prefix: Optional[str],
) -> None:
    _append_value(argv, "--plex-base-url", plex_base_url)
    _append_value(argv, "--plex-token", plex_token)
    _append_value(argv, "--plex-path-prefix", plex_path_prefix)
    _append_value(argv, "--local-path-prefix", local_path_prefix)


def _append_provider_credentials(
    argv: List[str],
    *,
    opensubtitles_api_key: Optional[str],
    opensubtitles_user_agent: Optional[str],
    opensubtitles_username: Optional[str],
    opensubtitles_password: Optional[str],
    opensubtitles_token: Optional[str],
    subdl_api_key: Optional[str],
) -> None:
    _append_value(argv, "--opensubtitles-api-key", opensubtitles_api_key)
    _append_value(argv, "--opensubtitles-user-agent", opensubtitles_user_agent)
    _append_value(argv, "--opensubtitles-username", opensubtitles_username)
    _append_value(argv, "--opensubtitles-password", opensubtitles_password)
    _append_value(argv, "--opensubtitles-token", opensubtitles_token)
    _append_value(argv, "--subdl-api-key", subdl_api_key)


def _job_runtime_argv(command: str) -> List[str]:
    return [command]


def _append_job_runtime(
    argv: List[str],
    *,
    job_store_dir: Optional[str],
    plex_base_url: Optional[str],
    plex_token: Optional[str],
    plex_path_prefix: Optional[str],
    local_path_prefix: Optional[str],
    openai_base_url: Optional[str],
    openai_api_key: Optional[str],
    allow_low_confidence_subtitle: bool,
    allow_provider_fallback_language: bool = False,
    opensubtitles_api_key: Optional[str],
    opensubtitles_user_agent: Optional[str],
    opensubtitles_username: Optional[str],
    opensubtitles_password: Optional[str],
    opensubtitles_token: Optional[str],
    subdl_api_key: Optional[str],
) -> None:
    _append_value(argv, "--job-store-dir", job_store_dir)
    _append_plex_connection(
        argv,
        plex_base_url=plex_base_url,
        plex_token=plex_token,
        plex_path_prefix=plex_path_prefix,
        local_path_prefix=local_path_prefix,
    )
    _append_value(argv, "--openai-base-url", openai_base_url)
    _append_value(argv, "--openai-api-key", openai_api_key)
    _append_flag(argv, "--allow-low-confidence-subtitle", allow_low_confidence_subtitle)
    _append_flag(argv, "--allow-provider-fallback-language", allow_provider_fallback_language)
    _append_provider_credentials(
        argv,
        opensubtitles_api_key=opensubtitles_api_key,
        opensubtitles_user_agent=opensubtitles_user_agent,
        opensubtitles_username=opensubtitles_username,
        opensubtitles_password=opensubtitles_password,
        opensubtitles_token=opensubtitles_token,
        subdl_api_key=subdl_api_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())
