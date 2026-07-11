from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from .env import load_project_dotenv
from .jobs import JobNeedsConfirmation, JobStore, JobStoreError, default_job_store_dir, run_job
from .local_resolver import local_search_by_title
from .planner import build_subtitle_plan
from .plex_resolver import PathMapping, PlexApiClient, PlexConnection, PlexResolver, PlexResolverError
from .languages import language_to_code
from .provider_policy import (
    DEFAULT_DOWNLOAD_PROVIDER_PRIORITY,
    LowConfidenceSubtitleCandidatesError,
    ProviderFallbackLanguageError,
    download_first_provider_candidate,
    parse_provider_priority,
    provider_names_for_search,
    rank_candidates_for_download,
)
from .providers.base import SubtitleCandidate, SubtitleProviderConfigurationError, SubtitleProviderError, SubtitleSearchRequest
from .providers.opensubtitles import OpenSubtitlesConfig, OpenSubtitlesProvider
from .providers.subdl import SubDLConfig, SubDLProvider
from .season_workflow import translate_plex_season
from .subtitle_matching import parse_release_info
from .workflow import WorkflowOptions, summary_json, translate_plex_resolved, translate_srt_file, translate_video_file


RELEASE_MATCH_PROVIDER_SEARCH_MIN_LIMIT = 30


def _env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        for env_name in _mpilot_env_aliases(name) + [name]:
            value = os.environ.get(env_name)
            if value is not None:
                return value
    return default


def _mpilot_env_aliases(name: str) -> list[str]:
    exact = {
        "BABELARR_JOB_STORE_DIR": "MPILOT_SUBTITLE_JOB_STORE_DIR",
        "BABELARR_PLEX_PATH_PREFIX": "MPILOT_PLEX_PATH_PREFIX",
        "BABELARR_LOCAL_PATH_PREFIX": "MPILOT_LOCAL_PATH_PREFIX",
        "BABELARR_SOURCE_LANGUAGE": "MPILOT_SUBTITLE_SOURCE_LANGUAGE",
        "BABELARR_TARGET_LANGUAGE": "MPILOT_SUBTITLE_TARGET_LANGUAGE",
    }
    if name in exact:
        return [exact[name]]
    if name.startswith("BABELARR_JOB_NOTIFICATION_"):
        return ["MPILOT_SUBTITLE_JOB_NOTIFICATION_" + name.removeprefix("BABELARR_JOB_NOTIFICATION_")]
    if name.startswith("BABELARR_RUNTIME_"):
        return ["MPILOT_RUNTIME_" + name.removeprefix("BABELARR_RUNTIME_")]
    if name.startswith("BABELARR_SOURCE_"):
        return ["MPILOT_SOURCE_" + name.removeprefix("BABELARR_SOURCE_")]
    if name.startswith("BABELARR_SUBTITLE_"):
        return ["MPILOT_SUBTITLE_" + name.removeprefix("BABELARR_SUBTITLE_")]
    if name.startswith("BABELARR_"):
        return ["MPILOT_SUBTITLE_" + name.removeprefix("BABELARR_")]
    return []


def _env_first_int(*names: str, default: int) -> int:
    value = _env_first(*names)
    if value is None or str(value).strip() == "":
        return default
    return int(value)


def _env_first_float(*names: str, default: float) -> float:
    value = _env_first(*names)
    if value is None or str(value).strip() == "":
        return default
    return float(value)


def add_common_options(
    parser: argparse.ArgumentParser,
    *,
    require_languages: bool = True,
    default_source_language: Optional[str] = None,
    default_target_language: Optional[str] = None,
    default_output_mode: str = "single-srt",
    include_output: bool = True,
) -> None:
    env = os.environ
    parser.add_argument(
        "--source-language",
        required=require_languages,
        default=default_source_language,
        help="Source language label or code, e.g. en, English, fr.",
    )
    parser.add_argument(
        "--target-language",
        required=require_languages,
        default=default_target_language,
        help="Target/primary language label or code, e.g. zh, French.",
    )
    parser.add_argument("--backend", choices=["fake", "codex-cli", "openai-compatible"], default=_env_first("BABELARR_BACKEND", "SUBTRANS_BACKEND", default="codex-cli"))
    parser.add_argument("--model", default=_env_first("BABELARR_MODEL", "SUBTRANS_MODEL", default="gpt-5.4-mini"))
    parser.add_argument("--output-mode", choices=["single-srt", "bilingual-ass"], default=default_output_mode)
    if include_output:
        parser.add_argument("--output", type=Path, help="Output sidecar path. Defaults to Plex-compatible naming.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing generated output.")
    parser.add_argument("--work-dir", type=Path, help="Working directory for extracted subtitles and model chunk artifacts.")
    parser.add_argument("--keep-work-dir", action="store_true", help="Keep temporary work files for debugging.")
    parser.add_argument(
        "--assume-unlabeled-stream-language",
        action="store_true",
        help="If exactly one embedded text subtitle stream has no language/title tags, treat it as --source-language.",
    )
    parser.add_argument("--primary-script", choices=["cjk", "latin"], default=_env_first("BABELARR_ASS_PRIMARY_SCRIPT", "SUBTRANS_ASS_PRIMARY_SCRIPT", default="cjk"))
    parser.add_argument("--secondary-script", choices=["cjk", "latin"], default=_env_first("BABELARR_ASS_SECONDARY_SCRIPT", "SUBTRANS_ASS_SECONDARY_SCRIPT", default="latin"))
    parser.add_argument("--ass-height", type=int, default=_env_first_int("BABELARR_ASS_HEIGHT", "SUBTRANS_ASS_HEIGHT", default=1080))
    parser.add_argument("--openai-base-url", default=env.get("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1"))
    parser.add_argument("--openai-api-key", default=env.get("OPENAI_API_KEY", "ollama"))


def add_plex_identifier_options(parser: argparse.ArgumentParser) -> None:
    identifier = parser.add_mutually_exclusive_group(required=True)
    identifier.add_argument("--imdb", help="IMDb ID, such as tt1234567.")
    identifier.add_argument("--rating-key", help="Plex ratingKey for a movie or episode.")
    parser.add_argument("--season", type=int, help="TV season number when resolving an episode.")
    parser.add_argument("--episode", type=int, help="TV episode number when resolving an episode.")


def add_plex_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--plex-base-url", default=os.environ.get("PLEX_BASE_URL"), help="Plex base URL. Defaults to PLEX_BASE_URL.")
    parser.add_argument("--plex-token", default=os.environ.get("PLEX_TOKEN"), help="Plex token. Defaults to PLEX_TOKEN.")
    parser.add_argument(
        "--plex-path-prefix",
        default=_env_first("BABELARR_PLEX_PATH_PREFIX", "MST_PLEX_PATH_PREFIX"),
        help="Plex/NAS path prefix to replace, e.g. '/server/media'.",
    )
    parser.add_argument(
        "--local-path-prefix",
        default=_env_first("BABELARR_LOCAL_PATH_PREFIX", "MST_LOCAL_PATH_PREFIX"),
        help="Local mount path prefix, e.g. '/mnt/media'.",
    )


def build_parser(prog: str = "mpilot subtitles") -> argparse.ArgumentParser:
    load_project_dotenv()
    default_download_priority = ",".join(DEFAULT_DOWNLOAD_PROVIDER_PRIORITY)
    default_provider_search_limit = _env_first_int("BABELARR_SUBTITLE_PROVIDER_SEARCH_LIMIT", "MST_SUBTITLE_PROVIDER_SEARCH_LIMIT", default=10)
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Standalone subtitle translation and sidecar automation for media libraries.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    srt_parser = subparsers.add_parser("translate-srt", help="Translate an existing SRT subtitle file.")
    srt_parser.add_argument("input", type=Path, help="Input source SRT path.")
    add_common_options(srt_parser)

    video_parser = subparsers.add_parser("translate-video", help="Acquire source subtitles from a media file, then translate.")
    video_parser.add_argument("input", type=Path, help="Input video/media path.")
    add_common_options(video_parser)

    plex_parser = subparsers.add_parser("plex-resolve", help="Resolve a Plex movie or episode to its media file path.")
    add_plex_identifier_options(plex_parser)
    add_plex_connection_options(plex_parser)

    plex_search_parser = subparsers.add_parser("plex-search", help="Search Plex by title and return local media candidates.")
    plex_search_parser.add_argument("--query", required=True, help="Movie or show title to search in Plex.")
    plex_search_parser.add_argument("--year", type=int, help="Optional release year filter for movie/show matches.")
    plex_search_parser.add_argument("--season", type=int, help="TV season number when resolving an episode.")
    plex_search_parser.add_argument("--episode", type=int, help="TV episode number when resolving an episode.")
    plex_search_parser.add_argument("--limit", type=int, default=10, help="Maximum Plex search results to inspect. Defaults to 10.")
    add_plex_connection_options(plex_search_parser)

    subtitle_plan_parser = subparsers.add_parser("subtitle-plan", help="Plan local subtitle availability for a Plex movie or episode.")
    add_plex_identifier_options(subtitle_plan_parser)
    add_plex_connection_options(subtitle_plan_parser)
    subtitle_plan_parser.add_argument("--target-language", required=True, help="Desired subtitle language label or code, e.g. zh, en, fr.")
    subtitle_plan_parser.add_argument(
        "--preferred-source-language",
        default=_env_first("BABELARR_PREFERRED_SOURCE_LANGUAGE", "SUBTRANS_PREFERRED_SOURCE_LANGUAGE", default="en"),
        help="Preferred source language for translation proposals. Defaults to en.",
    )

    subtitle_search_parser = subparsers.add_parser("subtitle-search", help="Search online subtitle providers and emit JSON candidates.")
    subtitle_search_parser.add_argument(
        "--provider",
        choices=["opensubtitles", "subdl", "all"],
        default=_env_first("BABELARR_SUBTITLE_PROVIDER", "MST_SUBTITLE_PROVIDER", default="all"),
        help="Subtitle provider to query. Defaults to all.",
    )
    subtitle_search_parser.add_argument("--imdb", help="IMDb ID, such as tt1234567.")
    subtitle_search_parser.add_argument("--tmdb", dest="tmdb_id", help="TMDb ID if known.")
    subtitle_search_parser.add_argument("--title", help="Movie or show title for keyword search.")
    subtitle_search_parser.add_argument("--file-name", help="Release or media file name for provider matching.")
    subtitle_search_parser.add_argument("--year", type=int, help="Release year.")
    subtitle_search_parser.add_argument("--media-type", choices=["movie", "episode", "tv"], help="Media type hint for providers.")
    subtitle_search_parser.add_argument("--season", type=int, help="TV season number.")
    subtitle_search_parser.add_argument("--episode", type=int, help="TV episode number.")
    subtitle_search_parser.add_argument(
        "--language",
        dest="languages",
        action="append",
        help="Preferred subtitle language. Can be repeated. Omit to search any language supported by the provider.",
    )
    subtitle_search_parser.add_argument("--limit", type=int, default=10, help="Maximum candidates per provider. Defaults to 10.")
    subtitle_search_parser.add_argument(
        "--download-provider-priority",
        default=_env_first("BABELARR_SUBTITLE_DOWNLOAD_PRIORITY", "MST_SUBTITLE_DOWNLOAD_PRIORITY", default=default_download_priority),
        help="Comma-separated provider download priority for result ordering. Defaults to subdl,opensubtitles.",
    )
    subtitle_search_parser.add_argument(
        "--opensubtitles-api-key",
        default=os.environ.get("OPENSUBTITLES_API_KEY"),
        help="OpenSubtitles.com API key. Defaults to OPENSUBTITLES_API_KEY.",
    )
    subtitle_search_parser.add_argument(
        "--opensubtitles-user-agent",
        default=os.environ.get("OPENSUBTITLES_USER_AGENT"),
        help="OpenSubtitles.com User-Agent. Defaults to OPENSUBTITLES_USER_AGENT or a project default.",
    )
    subtitle_search_parser.add_argument(
        "--opensubtitles-username",
        default=os.environ.get("OPENSUBTITLES_USERNAME"),
        help="OpenSubtitles.com username. Defaults to OPENSUBTITLES_USERNAME.",
    )
    subtitle_search_parser.add_argument(
        "--opensubtitles-password",
        default=os.environ.get("OPENSUBTITLES_PASSWORD"),
        help="OpenSubtitles.com password. Defaults to OPENSUBTITLES_PASSWORD.",
    )
    subtitle_search_parser.add_argument(
        "--opensubtitles-token",
        default=os.environ.get("OPENSUBTITLES_TOKEN"),
        help="Optional OpenSubtitles bearer token for authenticated account requests.",
    )
    subtitle_search_parser.add_argument(
        "--subdl-api-key",
        default=os.environ.get("SUBDL_API_KEY"),
        help="SubDL API key. Defaults to SUBDL_API_KEY.",
    )

    subtitle_fetch_parser = subparsers.add_parser(
        "subtitle-fetch",
        help="Search online subtitle providers and download the first candidate by provider priority.",
    )
    subtitle_fetch_parser.add_argument(
        "--provider",
        choices=["opensubtitles", "subdl", "all"],
        default=_env_first("BABELARR_SUBTITLE_PROVIDER", "MST_SUBTITLE_PROVIDER", default="all"),
        help="Subtitle provider to query. Defaults to all.",
    )
    subtitle_fetch_parser.add_argument("--imdb", help="IMDb ID, such as tt1234567.")
    subtitle_fetch_parser.add_argument("--tmdb", dest="tmdb_id", help="TMDb ID if known.")
    subtitle_fetch_parser.add_argument("--title", help="Movie or show title for keyword search.")
    subtitle_fetch_parser.add_argument("--file-name", help="Release or media file name for provider matching.")
    subtitle_fetch_parser.add_argument("--year", type=int, help="Release year.")
    subtitle_fetch_parser.add_argument("--media-type", choices=["movie", "episode", "tv"], help="Media type hint for providers.")
    subtitle_fetch_parser.add_argument("--season", type=int, help="TV season number.")
    subtitle_fetch_parser.add_argument("--episode", type=int, help="TV episode number.")
    subtitle_fetch_parser.add_argument(
        "--language",
        dest="languages",
        action="append",
        help="Preferred subtitle language. Can be repeated. Omit to search any language supported by the provider.",
    )
    subtitle_fetch_parser.add_argument("--limit", type=int, default=10, help="Maximum candidates per provider. Defaults to 10.")
    subtitle_fetch_parser.add_argument("--output-dir", type=Path, required=True, help="Directory for downloaded subtitle files.")
    subtitle_fetch_parser.add_argument("--force", action="store_true", help="Overwrite an existing downloaded file.")
    subtitle_fetch_parser.add_argument(
        "--allow-low-confidence-subtitle",
        action="store_true",
        help="Allow downloading a low-confidence release match after user confirmation.",
    )
    subtitle_fetch_parser.add_argument(
        "--download-provider-priority",
        default=_env_first("BABELARR_SUBTITLE_DOWNLOAD_PRIORITY", "MST_SUBTITLE_DOWNLOAD_PRIORITY", default=default_download_priority),
        help="Comma-separated provider download priority. Defaults to subdl,opensubtitles.",
    )
    subtitle_fetch_parser.add_argument(
        "--opensubtitles-api-key",
        default=os.environ.get("OPENSUBTITLES_API_KEY"),
        help="OpenSubtitles.com API key. Defaults to OPENSUBTITLES_API_KEY.",
    )
    subtitle_fetch_parser.add_argument(
        "--opensubtitles-user-agent",
        default=os.environ.get("OPENSUBTITLES_USER_AGENT"),
        help="OpenSubtitles.com User-Agent. Defaults to OPENSUBTITLES_USER_AGENT or a project default.",
    )
    subtitle_fetch_parser.add_argument(
        "--opensubtitles-username",
        default=os.environ.get("OPENSUBTITLES_USERNAME"),
        help="OpenSubtitles.com username. Defaults to OPENSUBTITLES_USERNAME.",
    )
    subtitle_fetch_parser.add_argument(
        "--opensubtitles-password",
        default=os.environ.get("OPENSUBTITLES_PASSWORD"),
        help="OpenSubtitles.com password. Defaults to OPENSUBTITLES_PASSWORD.",
    )
    subtitle_fetch_parser.add_argument(
        "--opensubtitles-token",
        default=os.environ.get("OPENSUBTITLES_TOKEN"),
        help="Optional OpenSubtitles bearer token for authenticated account requests.",
    )
    subtitle_fetch_parser.add_argument(
        "--subdl-api-key",
        default=os.environ.get("SUBDL_API_KEY"),
        help="SubDL API key. Defaults to SUBDL_API_KEY.",
    )

    subtitle_download_parser = subparsers.add_parser("subtitle-download", help="Download one online subtitle candidate to a local directory.")
    subtitle_download_parser.add_argument(
        "--provider",
        choices=["opensubtitles", "subdl"],
        required=True,
        help="Subtitle provider that owns the download identifier.",
    )
    subtitle_download_parser.add_argument("--file-id", help="OpenSubtitles file_id from subtitle-search JSON.")
    subtitle_download_parser.add_argument("--url", help="Direct download URL from subtitle-search JSON, used by SubDL.")
    subtitle_download_parser.add_argument("--file-name", help="Suggested output filename from subtitle-search JSON.")
    subtitle_download_parser.add_argument("--language", help="Subtitle language code for JSON metadata.")
    subtitle_download_parser.add_argument("--output-dir", type=Path, default=Path("."), help="Directory for downloaded subtitle files.")
    subtitle_download_parser.add_argument("--force", action="store_true", help="Overwrite an existing downloaded file.")
    subtitle_download_parser.add_argument(
        "--opensubtitles-api-key",
        default=os.environ.get("OPENSUBTITLES_API_KEY"),
        help="OpenSubtitles.com API key. Defaults to OPENSUBTITLES_API_KEY.",
    )
    subtitle_download_parser.add_argument(
        "--opensubtitles-user-agent",
        default=os.environ.get("OPENSUBTITLES_USER_AGENT"),
        help="OpenSubtitles.com User-Agent. Defaults to OPENSUBTITLES_USER_AGENT or a project default.",
    )
    subtitle_download_parser.add_argument(
        "--opensubtitles-username",
        default=os.environ.get("OPENSUBTITLES_USERNAME"),
        help="OpenSubtitles.com username. Defaults to OPENSUBTITLES_USERNAME.",
    )
    subtitle_download_parser.add_argument(
        "--opensubtitles-password",
        default=os.environ.get("OPENSUBTITLES_PASSWORD"),
        help="OpenSubtitles.com password. Defaults to OPENSUBTITLES_PASSWORD.",
    )
    subtitle_download_parser.add_argument(
        "--opensubtitles-token",
        default=os.environ.get("OPENSUBTITLES_TOKEN"),
        help="Optional OpenSubtitles bearer token for authenticated account requests.",
    )
    subtitle_download_parser.add_argument(
        "--subdl-api-key",
        default=os.environ.get("SUBDL_API_KEY"),
        help="SubDL API key. Defaults to SUBDL_API_KEY.",
    )

    translate_plex_parser = subparsers.add_parser(
        "translate-plex",
        help="Resolve a Plex movie or episode, then translate it with the video workflow.",
    )
    add_plex_identifier_options(translate_plex_parser)
    add_plex_connection_options(translate_plex_parser)
    add_common_options(
        translate_plex_parser,
        require_languages=False,
        default_source_language=_env_first("BABELARR_SOURCE_LANGUAGE", "SUBTRANS_SOURCE_LANGUAGE", default="en"),
        default_target_language=_env_first("BABELARR_TARGET_LANGUAGE", "SUBTRANS_TARGET_LANGUAGE", default="zh"),
        default_output_mode=_env_first("BABELARR_OUTPUT_MODE", "SUBTRANS_OUTPUT_MODE", default="bilingual-ass"),
    )
    translate_plex_parser.add_argument(
        "--no-online-subtitle-fallback",
        action="store_true",
        help="Disable online provider fallback when local source subtitles are missing.",
    )
    translate_plex_parser.add_argument(
        "--write-back",
        action="store_true",
        help="Write the generated subtitle next to the resolved Plex media file.",
    )
    translate_plex_parser.add_argument(
        "--refresh-plex",
        action="store_true",
        help="After --write-back succeeds, ask Plex to scan the media folder for the new sidecar.",
    )
    translate_plex_parser.add_argument(
        "--allow-low-confidence-subtitle",
        action="store_true",
        help="Allow provider fallback to use a low-confidence release match after user confirmation.",
    )
    translate_plex_parser.add_argument(
        "--subtitle-provider",
        choices=["opensubtitles", "subdl", "all"],
        default=_env_first("BABELARR_SUBTITLE_PROVIDER", "MST_SUBTITLE_PROVIDER", default="all"),
        help="Subtitle provider to query for translate-plex fallback. Defaults to all.",
    )
    translate_plex_parser.add_argument(
        "--provider-search-limit",
        type=int,
        default=default_provider_search_limit,
        help="Maximum candidates per provider for translate-plex fallback. Defaults to BABELARR_SUBTITLE_PROVIDER_SEARCH_LIMIT, legacy MST_SUBTITLE_PROVIDER_SEARCH_LIMIT, or 10.",
    )
    translate_plex_parser.add_argument(
        "--download-provider-priority",
        default=_env_first("BABELARR_SUBTITLE_DOWNLOAD_PRIORITY", "MST_SUBTITLE_DOWNLOAD_PRIORITY", default=default_download_priority),
        help="Comma-separated provider download priority. Defaults to subdl,opensubtitles.",
    )
    translate_plex_parser.add_argument(
        "--opensubtitles-api-key",
        default=os.environ.get("OPENSUBTITLES_API_KEY"),
        help="OpenSubtitles.com API key. Defaults to OPENSUBTITLES_API_KEY.",
    )
    translate_plex_parser.add_argument(
        "--opensubtitles-user-agent",
        default=os.environ.get("OPENSUBTITLES_USER_AGENT"),
        help="OpenSubtitles.com User-Agent. Defaults to OPENSUBTITLES_USER_AGENT or a project default.",
    )
    translate_plex_parser.add_argument(
        "--opensubtitles-username",
        default=os.environ.get("OPENSUBTITLES_USERNAME"),
        help="OpenSubtitles.com username. Defaults to OPENSUBTITLES_USERNAME.",
    )
    translate_plex_parser.add_argument(
        "--opensubtitles-password",
        default=os.environ.get("OPENSUBTITLES_PASSWORD"),
        help="OpenSubtitles.com password. Defaults to OPENSUBTITLES_PASSWORD.",
    )
    translate_plex_parser.add_argument(
        "--opensubtitles-token",
        default=os.environ.get("OPENSUBTITLES_TOKEN"),
        help="Optional OpenSubtitles bearer token for authenticated account requests.",
    )
    translate_plex_parser.add_argument(
        "--subdl-api-key",
        default=os.environ.get("SUBDL_API_KEY"),
        help="SubDL API key. Defaults to SUBDL_API_KEY.",
    )

    translate_plex_season_parser = subparsers.add_parser(
        "translate-plex-season",
        help="Resolve and translate a bounded batch of episodes from one Plex season.",
    )
    season_identifier = translate_plex_season_parser.add_mutually_exclusive_group(required=True)
    season_identifier.add_argument("--imdb", help="IMDb ID for the show, such as tt1234567.")
    season_identifier.add_argument("--rating-key", help="Plex ratingKey for a show.")
    translate_plex_season_parser.add_argument("--season", type=int, required=True, help="TV season number.")
    translate_plex_season_parser.add_argument("--episode-start", type=int, required=True, help="First episode number in the requested range.")
    translate_plex_season_parser.add_argument("--episode-end", type=int, required=True, help="Last episode number in the requested range.")
    translate_plex_season_parser.add_argument(
        "--batch-size",
        type=int,
        default=3,
        help="Maximum episodes to process in this invocation. Defaults to 3 and cannot exceed 3.",
    )
    add_plex_connection_options(translate_plex_season_parser)
    add_common_options(
        translate_plex_season_parser,
        require_languages=False,
        default_source_language=_env_first("BABELARR_SOURCE_LANGUAGE", "SUBTRANS_SOURCE_LANGUAGE", default="en"),
        default_target_language=_env_first("BABELARR_TARGET_LANGUAGE", "SUBTRANS_TARGET_LANGUAGE", default="zh"),
        default_output_mode=_env_first("BABELARR_OUTPUT_MODE", "SUBTRANS_OUTPUT_MODE", default="bilingual-ass"),
        include_output=False,
    )
    translate_plex_season_parser.add_argument("--no-online-subtitle-fallback", action="store_true")
    translate_plex_season_parser.add_argument("--write-back", action="store_true")
    translate_plex_season_parser.add_argument("--refresh-plex", action="store_true")
    translate_plex_season_parser.add_argument("--allow-low-confidence-subtitle", action="store_true")
    translate_plex_season_parser.add_argument(
        "--subtitle-provider",
        choices=["opensubtitles", "subdl", "all"],
        default=_env_first("BABELARR_SUBTITLE_PROVIDER", "MST_SUBTITLE_PROVIDER", default="all"),
    )
    translate_plex_season_parser.add_argument(
        "--provider-search-limit",
        type=int,
        default=default_provider_search_limit,
    )
    translate_plex_season_parser.add_argument(
        "--download-provider-priority",
        default=_env_first("BABELARR_SUBTITLE_DOWNLOAD_PRIORITY", "MST_SUBTITLE_DOWNLOAD_PRIORITY", default=default_download_priority),
    )
    add_provider_credential_options(translate_plex_season_parser)

    job_create_parser = subparsers.add_parser("job-create", help="Create a persistent translate-plex job record.")
    add_job_store_option(job_create_parser)
    add_translate_plex_job_options(job_create_parser)

    job_create_video_parser = subparsers.add_parser("job-create-video", help="Create a persistent translate-video job from a local file path.")
    add_job_store_option(job_create_video_parser)
    add_translate_video_job_options(job_create_video_parser)

    job_show_parser = subparsers.add_parser("job-show", help="Show one persistent job record as JSON.")
    add_job_store_option(job_show_parser)
    job_show_parser.add_argument("job_id", help="Job ID returned by job-create.")

    job_list_parser = subparsers.add_parser("job-list", help="List persistent job records.")
    add_job_store_option(job_list_parser)
    job_list_parser.add_argument("--status", choices=["queued", "running", "succeeded", "failed", "needs_confirmation"])
    job_list_parser.add_argument("--limit", type=int, default=50)

    job_run_parser = subparsers.add_parser("job-run", help="Run or retry one persistent job.")
    add_job_store_option(job_run_parser)
    job_run_parser.add_argument("job_id", help="Job ID returned by job-create.")
    add_plex_connection_options(job_run_parser)
    job_run_parser.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1"))
    job_run_parser.add_argument("--openai-api-key", default=os.environ.get("OPENAI_API_KEY", "ollama"))
    job_run_parser.add_argument(
        "--allow-low-confidence-subtitle",
        action="store_true",
        help="Confirm and allow a low-confidence provider subtitle for this run.",
    )
    job_run_parser.add_argument(
        "--allow-provider-fallback-language",
        action="store_true",
        help="Confirm and allow a provider subtitle in a fallback language for this run.",
    )
    _add_runtime_mirror_options(job_run_parser)
    add_provider_credential_options(job_run_parser)

    job_start_parser = subparsers.add_parser("job-start", help="Start one persistent job in a background worker.")
    add_job_store_option(job_start_parser)
    job_start_parser.add_argument("job_id", help="Job ID returned by job-create.")
    add_plex_connection_options(job_start_parser)
    job_start_parser.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1"))
    job_start_parser.add_argument("--openai-api-key", default=os.environ.get("OPENAI_API_KEY", "ollama"))
    job_start_parser.add_argument(
        "--allow-low-confidence-subtitle",
        action="store_true",
        help="Confirm and allow a low-confidence provider subtitle for this run.",
    )
    job_start_parser.add_argument(
        "--allow-provider-fallback-language",
        action="store_true",
        help="Confirm and allow a provider subtitle in a fallback language for this run.",
    )
    job_start_parser.add_argument("--notification-target", help="Hermes target for running status and terminal notices.")
    job_start_parser.add_argument("--requester-id", help="Requester identity to resolve into a notification target.")
    job_start_parser.add_argument("--notification-title", help="User-facing title for subtitle job notifications.")
    job_start_parser.add_argument("--notification-language", help="Notification language, such as zh, en, or fr.")
    _add_runtime_mirror_options(job_start_parser)
    add_provider_credential_options(job_start_parser)

    job_resume_parser = subparsers.add_parser("job-resume", help="Run recoverable queued, failed, or stale running jobs.")
    add_job_store_option(job_resume_parser)
    add_plex_connection_options(job_resume_parser)
    job_resume_parser.add_argument("--openai-base-url", default=os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:11434/v1"))
    job_resume_parser.add_argument("--openai-api-key", default=os.environ.get("OPENAI_API_KEY", "ollama"))
    job_resume_parser.add_argument("--stale-after-seconds", type=int, default=3600)
    job_resume_parser.add_argument("--limit", type=int, default=10)
    job_resume_parser.add_argument(
        "--allow-low-confidence-subtitle",
        action="store_true",
        help="Confirm and allow low-confidence provider subtitles while resuming.",
    )
    add_provider_credential_options(job_resume_parser)

    job_prune_parser = subparsers.add_parser("job-prune", help="Delete old completed job records from the persistent job store.")
    add_job_store_option(job_prune_parser)
    job_prune_parser.add_argument(
        "--retention-days",
        type=int,
        default=90,
        help="Keep completed job records newer than this many days. Defaults to 90.",
    )
    job_prune_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report prune candidates without deleting job files.",
    )

    notify_daemon_parser = subparsers.add_parser("notify-daemon", help="Run the single notification sender daemon.")
    notify_daemon_parser.add_argument(
        "--idle-exit-seconds",
        type=float,
        default=_env_first_float("BABELARR_JOB_NOTIFICATION_DAEMON_IDLE_EXIT_SECONDS", "MST_JOB_NOTIFICATION_DAEMON_IDLE_EXIT_SECONDS", default=300.0),
        help="Exit after this many seconds without pending watches. Defaults to 300.",
    )
    notify_daemon_parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=None,
        help="Override the notification poll interval for this daemon.",
    )
    notify_daemon_parser.add_argument(
        "--lock-acquire-timeout-seconds",
        type=float,
        default=_env_first_float("BABELARR_JOB_NOTIFICATION_DAEMON_LOCK_TIMEOUT_SECONDS", "MST_JOB_NOTIFICATION_DAEMON_LOCK_TIMEOUT_SECONDS", default=3.0),
        help="Wait this long for an exiting daemon to release the singleton lock before giving up. Defaults to 3.",
    )
    notify_daemon_parser.add_argument(
        "--once",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def add_job_store_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--job-store-dir",
        type=Path,
        default=default_job_store_dir(),
        help=(
            "Persistent job store directory. Defaults to MPILOT_SUBTITLE_JOB_STORE_DIR, "
            "BABELARR_JOB_STORE_DIR, MST_JOB_STORE_DIR, or ~/.local/share/mpilot/subtitles/jobs."
        ),
    )


def add_provider_credential_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--opensubtitles-api-key",
        default=os.environ.get("OPENSUBTITLES_API_KEY"),
        help="OpenSubtitles.com API key. Defaults to OPENSUBTITLES_API_KEY.",
    )
    parser.add_argument(
        "--opensubtitles-user-agent",
        default=os.environ.get("OPENSUBTITLES_USER_AGENT"),
        help="OpenSubtitles.com User-Agent. Defaults to OPENSUBTITLES_USER_AGENT or a project default.",
    )
    parser.add_argument(
        "--opensubtitles-username",
        default=os.environ.get("OPENSUBTITLES_USERNAME"),
        help="OpenSubtitles.com username. Defaults to OPENSUBTITLES_USERNAME.",
    )
    parser.add_argument(
        "--opensubtitles-password",
        default=os.environ.get("OPENSUBTITLES_PASSWORD"),
        help="OpenSubtitles.com password. Defaults to OPENSUBTITLES_PASSWORD.",
    )
    parser.add_argument(
        "--opensubtitles-token",
        default=os.environ.get("OPENSUBTITLES_TOKEN"),
        help="Optional OpenSubtitles bearer token for authenticated account requests.",
    )
    parser.add_argument(
        "--subdl-api-key",
        default=os.environ.get("SUBDL_API_KEY"),
        help="SubDL API key. Defaults to SUBDL_API_KEY.",
    )


def add_translate_plex_job_options(parser: argparse.ArgumentParser) -> None:
    env = os.environ
    add_plex_identifier_options(parser)
    parser.add_argument("--plex-base-url", default=env.get("PLEX_BASE_URL"), help="Plex base URL to store with the job.")
    parser.add_argument(
        "--plex-path-prefix",
        default=_env_first("BABELARR_PLEX_PATH_PREFIX", "MST_PLEX_PATH_PREFIX"),
        help="Plex/NAS path prefix to store with the job.",
    )
    parser.add_argument(
        "--local-path-prefix",
        default=_env_first("BABELARR_LOCAL_PATH_PREFIX", "MST_LOCAL_PATH_PREFIX"),
        help="Local mount path prefix to store with the job.",
    )
    parser.add_argument("--source-language", default=_env_first("BABELARR_SOURCE_LANGUAGE", "SUBTRANS_SOURCE_LANGUAGE", default="en"))
    parser.add_argument("--target-language", default=_env_first("BABELARR_TARGET_LANGUAGE", "SUBTRANS_TARGET_LANGUAGE", default="zh"))
    parser.add_argument("--backend", choices=["fake", "codex-cli", "openai-compatible"], default=_env_first("BABELARR_BACKEND", "SUBTRANS_BACKEND", default="codex-cli"))
    parser.add_argument("--model", default=_env_first("BABELARR_MODEL", "SUBTRANS_MODEL", default="gpt-5.4-mini"))
    parser.add_argument("--output-mode", choices=["single-srt", "bilingual-ass"], default=_env_first("BABELARR_OUTPUT_MODE", "SUBTRANS_OUTPUT_MODE", default="bilingual-ass"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument(
        "--assume-unlabeled-stream-language",
        action="store_true",
        help="If exactly one embedded text subtitle stream has no language/title tags, treat it as --source-language.",
    )
    parser.add_argument("--primary-script", choices=["cjk", "latin"], default=_env_first("BABELARR_ASS_PRIMARY_SCRIPT", "SUBTRANS_ASS_PRIMARY_SCRIPT", default="cjk"))
    parser.add_argument("--secondary-script", choices=["cjk", "latin"], default=_env_first("BABELARR_ASS_SECONDARY_SCRIPT", "SUBTRANS_ASS_SECONDARY_SCRIPT", default="latin"))
    parser.add_argument("--ass-height", type=int, default=_env_first_int("BABELARR_ASS_HEIGHT", "SUBTRANS_ASS_HEIGHT", default=1080))
    parser.add_argument("--write-back", action="store_true")
    parser.add_argument("--refresh-plex", action="store_true")
    parser.add_argument("--no-online-subtitle-fallback", action="store_true")
    parser.add_argument("--allow-low-confidence-subtitle", action="store_true")
    parser.add_argument("--allow-provider-fallback-language", action="store_true")
    parser.add_argument(
        "--subtitle-provider",
        choices=["opensubtitles", "subdl", "all"],
        default=_env_first("BABELARR_SUBTITLE_PROVIDER", "MST_SUBTITLE_PROVIDER", default="all"),
    )
    parser.add_argument(
        "--provider-search-limit",
        type=int,
        default=_env_first_int("BABELARR_SUBTITLE_PROVIDER_SEARCH_LIMIT", "MST_SUBTITLE_PROVIDER_SEARCH_LIMIT", default=10),
    )
    parser.add_argument(
        "--download-provider-priority",
        default=_env_first("BABELARR_SUBTITLE_DOWNLOAD_PRIORITY", "MST_SUBTITLE_DOWNLOAD_PRIORITY", default=",".join(DEFAULT_DOWNLOAD_PROVIDER_PRIORITY)),
    )


def add_translate_video_job_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--video-path", required=True, type=Path, help="Absolute path to the video file to translate.")
    parser.add_argument("--imdb-id", default=None, help="Optional IMDb ID for online subtitle provider search (e.g. tt1234567).")
    parser.add_argument("--title", default=None, help="Optional media title for subtitle provider search.")
    parser.add_argument("--media-type", choices=["movie", "episode"], default="movie", help="Media type for subtitle provider search.")
    parser.add_argument("--season", type=int, help="TV season number for episode subtitle provider search.")
    parser.add_argument("--episode", type=int, help="TV episode number for episode subtitle provider search.")
    parser.add_argument("--source-language", default=_env_first("BABELARR_SOURCE_LANGUAGE", "SUBTRANS_SOURCE_LANGUAGE", default="en"))
    parser.add_argument("--target-language", default=_env_first("BABELARR_TARGET_LANGUAGE", "SUBTRANS_TARGET_LANGUAGE", default="zh"))
    parser.add_argument("--backend", choices=["fake", "codex-cli", "openai-compatible"], default=_env_first("BABELARR_BACKEND", "SUBTRANS_BACKEND", default="codex-cli"))
    parser.add_argument("--model", default=_env_first("BABELARR_MODEL", "SUBTRANS_MODEL", default="gpt-5.4-mini"))
    parser.add_argument("--output-mode", choices=["single-srt", "bilingual-ass"], default=_env_first("BABELARR_OUTPUT_MODE", "SUBTRANS_OUTPUT_MODE", default="bilingual-ass"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--keep-work-dir", action="store_true")
    parser.add_argument(
        "--assume-unlabeled-stream-language",
        action="store_true",
        help="If exactly one embedded text subtitle stream has no language/title tags, treat it as --source-language.",
    )
    parser.add_argument("--primary-script", choices=["cjk", "latin"], default=_env_first("BABELARR_ASS_PRIMARY_SCRIPT", "SUBTRANS_ASS_PRIMARY_SCRIPT", default="cjk"))
    parser.add_argument("--secondary-script", choices=["cjk", "latin"], default=_env_first("BABELARR_ASS_SECONDARY_SCRIPT", "SUBTRANS_ASS_SECONDARY_SCRIPT", default="latin"))
    parser.add_argument("--ass-height", type=int, default=_env_first_int("BABELARR_ASS_HEIGHT", "SUBTRANS_ASS_HEIGHT", default=1080))
    parser.add_argument("--no-online-subtitle-fallback", action="store_true")
    parser.add_argument("--allow-low-confidence-subtitle", action="store_true")
    parser.add_argument("--allow-provider-fallback-language", action="store_true")
    parser.add_argument(
        "--subtitle-provider",
        choices=["opensubtitles", "subdl", "all"],
        default=_env_first("BABELARR_SUBTITLE_PROVIDER", "MST_SUBTITLE_PROVIDER", default="all"),
    )
    parser.add_argument(
        "--provider-search-limit",
        type=int,
        default=_env_first_int("BABELARR_SUBTITLE_PROVIDER_SEARCH_LIMIT", "MST_SUBTITLE_PROVIDER_SEARCH_LIMIT", default=10),
    )
    parser.add_argument(
        "--download-provider-priority",
        default=_env_first("BABELARR_SUBTITLE_DOWNLOAD_PRIORITY", "MST_SUBTITLE_DOWNLOAD_PRIORITY", default=",".join(DEFAULT_DOWNLOAD_PROVIDER_PRIORITY)),
    )
    add_provider_credential_options(parser)


def options_from_args(args: argparse.Namespace, plex_client: Optional[PlexApiClient] = None) -> WorkflowOptions:
    online_subtitle_fetcher = None
    plex_refresher = None
    if getattr(args, "command", None) in {"translate-plex", "translate-plex-season", "translate-video"} and hasattr(args, "subtitle_provider") and not getattr(args, "no_online_subtitle_fallback", False):
        online_subtitle_fetcher = build_plex_online_subtitle_fetcher(args)
    if getattr(args, "command", None) in {"translate-plex", "translate-plex-season"} and getattr(args, "refresh_plex", False) and plex_client is not None:
        plex_refresher = build_plex_refresher(plex_client)
    return WorkflowOptions(
        source_language=args.source_language,
        target_language=args.target_language,
        output_mode=args.output_mode,
        backend=args.backend,
        model=args.model,
        output=getattr(args, "output", None),
        force=args.force,
        work_dir=args.work_dir,
        keep_work_dir=args.keep_work_dir,
        assume_unlabeled_stream_language=bool(getattr(args, "assume_unlabeled_stream_language", False)),
        primary_script=args.primary_script,
        secondary_script=args.secondary_script,
        ass_height=args.ass_height,
        openai_base_url=args.openai_base_url,
        openai_api_key=args.openai_api_key,
        online_subtitle_fetcher=online_subtitle_fetcher,
        write_back=getattr(args, "write_back", False),
        refresh_plex=getattr(args, "refresh_plex", False),
        plex_refresher=plex_refresher,
        progress_callback=getattr(args, "progress_callback", None),
    )


def subtitle_search_request_from_args(args: argparse.Namespace) -> SubtitleSearchRequest:
    return SubtitleSearchRequest(
        media_type=args.media_type,
        title=args.title,
        year=args.year,
        imdb_id=args.imdb,
        tmdb_id=args.tmdb_id,
        season=args.season,
        episode=args.episode,
        file_name=args.file_name,
        languages=tuple(args.languages or ()),
        limit=args.limit,
    )


def subtitle_search_summary(args: argparse.Namespace) -> Dict[str, Any]:
    request = subtitle_search_request_from_args(args)
    provider_request = provider_search_request_for_release_matching(request)
    download_priority = parse_provider_priority(args.download_provider_priority)
    provider_names = provider_names_for_search(args.provider)
    providers, candidates, _provider_instances = search_subtitle_providers(
        provider_request,
        provider_names,
        args,
        tolerate_provider_errors=args.provider == "all",
    )
    ranked_candidates = rank_candidates_for_download(candidates, download_priority, media_release_name=args.file_name)
    return {
        "query": request.to_dict(),
        "providers": providers,
        "download_provider_priority": download_priority,
        "results": [candidate.to_dict() for candidate in ranked_candidates[: request.limit]],
    }


def search_subtitle_providers(
    request: SubtitleSearchRequest,
    provider_names: List[str],
    args: argparse.Namespace,
    *,
    tolerate_provider_errors: bool,
):
    providers: List[Dict[str, Any]] = []
    results: List[SubtitleCandidate] = []
    provider_instances = {}
    for provider_name in provider_names:
        try:
            provider = build_subtitle_provider(provider_name, args)
        except SubtitleProviderConfigurationError as error:
            if tolerate_provider_errors:
                providers.append({"name": provider_name, "status": "skipped", "reason": str(error)})
                continue
            raise
        provider_instances[provider_name] = provider
        try:
            candidates = provider.search(request)
        except SubtitleProviderError as error:
            if tolerate_provider_errors:
                providers.append({"name": provider_name, "status": "error", "reason": str(error)})
                continue
            raise
        providers.append({"name": provider_name, "status": "ok", "count": len(candidates)})
        results.extend(candidates)
    return providers, results, provider_instances


def subtitle_fetch_summary(args: argparse.Namespace) -> Dict[str, Any]:
    request = subtitle_search_request_from_args(args)
    provider_request = provider_search_request_for_release_matching(request)
    download_priority = parse_provider_priority(args.download_provider_priority)
    provider_names = provider_names_for_search(args.provider)
    providers, candidates, provider_instances = search_subtitle_providers(
        provider_request,
        provider_names,
        args,
        tolerate_provider_errors=args.provider == "all",
    )
    try:
        selection = download_first_provider_candidate(
            candidates,
            provider_instances,
            args.output_dir,
            force=args.force,
            provider_priority=download_priority,
            media_release_name=args.file_name,
            target_season=request.season,
            target_episode=request.episode,
            allow_low_confidence=args.allow_low_confidence_subtitle,
        )
    except LowConfidenceSubtitleCandidatesError as error:
        return {
            "query": request.to_dict(),
            "providers": providers,
            "download_provider_priority": download_priority,
            "candidates_considered": len(candidates),
            "selected": None,
            "proposal": error.to_dict(),
        }
    return {
        "query": request.to_dict(),
        "providers": providers,
        "download_provider_priority": download_priority,
        "candidates_considered": len(candidates),
        "selected": selection.to_dict(),
    }


def subtitle_search_request_from_resolved_media(
    resolved_media,
    source_language: Optional[str],
    limit: int,
) -> SubtitleSearchRequest:
    media_type = "episode" if resolved_media.media_type == "episode" else "movie"
    title = resolved_media.show_title if media_type == "episode" and resolved_media.show_title else resolved_media.title
    return SubtitleSearchRequest(
        media_type=media_type,
        title=title,
        imdb_id=resolved_media.imdb,
        season=resolved_media.season,
        episode=resolved_media.episode,
        file_name=Path(resolved_media.local_file).name,
        languages=(source_language,) if source_language else (),
        limit=limit,
    )


def provider_search_request_for_release_matching(request: SubtitleSearchRequest) -> SubtitleSearchRequest:
    if not request.file_name or request.limit >= RELEASE_MATCH_PROVIDER_SEARCH_MIN_LIMIT:
        return request
    return replace(request, limit=RELEASE_MATCH_PROVIDER_SEARCH_MIN_LIMIT)


def _provider_search_stages(source_language: str) -> List[Optional[str]]:
    """Return ordered search languages: preferred → English fallback → any language."""
    try:
        source_code = language_to_code(source_language)
    except ValueError:
        source_code = source_language.lower()
    stages: List[Optional[str]] = [source_language]
    if source_code != "en":
        stages.append("en")
    stages.append(None)
    return stages


def build_plex_online_subtitle_fetcher(args: argparse.Namespace):
    provider_name = args.subtitle_provider
    provider_names = provider_names_for_search(provider_name)
    download_priority = parse_provider_priority(args.download_provider_priority)
    search_limit = args.provider_search_limit

    def fetcher(resolved_media, source_language: str, output_dir: Path, force: bool = False):
        tolerate_errors = provider_name == "all"
        stages = _provider_search_stages(source_language)
        candidates: List[SubtitleCandidate] = []
        provider_instances = {}
        fallback_stage: int = 0

        for stage_index, stage_language in enumerate(stages):
            request = subtitle_search_request_from_resolved_media(resolved_media, stage_language, search_limit)
            provider_request = provider_search_request_for_release_matching(request)
            _emit_args_progress(
                args,
                "searching_online_subtitles",
                "Searching third-party subtitle providers.",
                request=provider_request.to_dict(),
                providers=provider_names,
                search_stage=stage_index + 1,
            )
            _providers, stage_candidates, stage_instances = search_subtitle_providers(
                provider_request, provider_names, args, tolerate_provider_errors=tolerate_errors,
            )
            provider_instances.update(stage_instances)
            _emit_args_progress(
                args,
                "online_subtitle_candidates",
                "Third-party subtitle search completed.",
                providers=_providers,
                candidates_considered=len(stage_candidates),
                search_stage=stage_index + 1,
            )
            if stage_candidates:
                candidates = stage_candidates
                fallback_stage = stage_index
                break

        selection = download_first_provider_candidate(
            candidates,
            provider_instances,
            output_dir,
            force=force,
            provider_priority=download_priority,
            media_release_name=Path(resolved_media.local_file).name,
            target_season=resolved_media.season,
            target_episode=resolved_media.episode,
            allow_low_confidence=args.allow_low_confidence_subtitle,
        )
        _emit_args_progress(
            args,
            "online_subtitle_selected",
            "Selected a third-party source subtitle.",
            provider=selection.candidate.provider,
            language=selection.candidate.language,
            file_name=selection.candidate.file_name,
            candidates_considered=len(candidates),
            search_stage=fallback_stage + 1,
        )

        allow_fallback = bool(getattr(args, "allow_provider_fallback_language", False))
        if fallback_stage > 0 and not allow_fallback:
            found_language = selection.candidate.language or (stages[fallback_stage] or "unknown")
            raise ProviderFallbackLanguageError(
                selection=selection,
                requested_language=source_language,
                found_language=found_language,
                search_stage=fallback_stage + 1,
            )

        return selection

    return fetcher


def _emit_args_progress(args: argparse.Namespace, stage: str, message: str, **details) -> None:
    callback = getattr(args, "progress_callback", None)
    if callback is None:
        return
    try:
        callback(
            {
                "stage": stage,
                "message": message,
                "details": {key: value for key, value in details.items() if value is not None},
            }
        )
    except Exception:
        pass


def build_plex_refresher(client: PlexApiClient):
    def refresher(resolved_media, write_back_path: Path) -> Dict[str, Any]:
        if not resolved_media.library_section_id:
            return {
                "requested": True,
                "status": "skipped",
                "reason": "Plex metadata did not include librarySectionID",
                "write_back_path": str(write_back_path),
            }
        plex_directory = str(PurePosixPath(resolved_media.plex_file).parent)
        result = client.scan_library_path(resolved_media.library_section_id, plex_directory)
        result["requested"] = True
        result["write_back_path"] = str(write_back_path)
        return result

    return refresher


def subtitle_download_summary(args: argparse.Namespace) -> Dict[str, Any]:
    provider = build_subtitle_provider(args.provider, args)
    candidate = subtitle_download_candidate_from_args(args)
    downloaded = provider.download(candidate, args.output_dir, force=args.force)
    return {
        "provider": args.provider,
        "candidate": candidate.to_dict(),
        "download": downloaded.to_dict(),
    }


def subtitle_download_candidate_from_args(args: argparse.Namespace) -> SubtitleCandidate:
    if args.provider == "opensubtitles":
        if not args.file_id:
            raise ValueError("subtitle-download --provider opensubtitles requires --file-id")
        return SubtitleCandidate(
            provider="opensubtitles",
            provider_id=str(args.file_id),
            language=args.language,
            file_name=args.file_name,
            file_id=str(args.file_id),
            download={"method": "opensubtitles-download", "file_id": str(args.file_id), "requires_token": True},
        )
    if args.provider == "subdl":
        if not args.url:
            raise ValueError("subtitle-download --provider subdl requires --url")
        return SubtitleCandidate(
            provider="subdl",
            provider_id=str(args.url),
            language=args.language,
            file_name=args.file_name,
            download={"method": "direct-url", "url": str(args.url)},
        )
    raise ValueError("unknown subtitle provider: %s" % args.provider)


def job_create_summary(args: argparse.Namespace) -> Dict[str, Any]:
    store = JobStore(args.job_store_dir)
    job = store.create("translate-plex", job_request_from_args(args))
    return {
        "job_store": str(store.root),
        "job": job,
    }


def job_show_summary(args: argparse.Namespace) -> Dict[str, Any]:
    store = JobStore(args.job_store_dir)
    job = store.get(args.job_id)
    return {
        "job_store": str(store.root),
        "job": job,
        "status_detail": job_status_detail(job),
    }


def job_list_summary(args: argparse.Namespace) -> Dict[str, Any]:
    store = JobStore(args.job_store_dir)
    jobs = store.list(status=args.status, limit=args.limit)
    return {
        "job_store": str(store.root),
        "count": len(jobs),
        "jobs": jobs,
    }


def job_status_detail(job: Dict[str, Any]) -> Dict[str, Any]:
    request = _dict_or_empty(job.get("request"))
    translation_request = _dict_or_empty(request.get("translation"))
    result = _dict_or_empty(job.get("result"))
    translation_result = _dict_or_empty(result.get("translation"))
    progress = _dict_or_none(job.get("progress"))
    milestones = _dict_or_empty(job.get("progress_milestones"))
    status = str(job.get("status") or "")
    stage = str(progress.get("stage") or status or "unknown") if progress else (status or "unknown")
    detail: Dict[str, Any] = {
        "status": status,
        "stage": stage,
        "message": str(progress.get("message") or _default_job_status_message(status)) if progress else _default_job_status_message(status),
        "source_language": result.get("source_language") or translation_result.get("source_language") or translation_request.get("source_language"),
        "target_language": result.get("target_language") or translation_result.get("target_language") or translation_request.get("target_language"),
        "output_mode": result.get("output_mode") or translation_result.get("output_mode") or translation_request.get("output_mode"),
    }
    if progress:
        detail["current"] = progress
    if milestones:
        detail["milestones"] = milestones

    translation_progress = _job_translation_progress(progress, translation_result)
    if translation_progress:
        detail["translation_progress"] = translation_progress

    source_status = _job_source_status(milestones, translation_result)
    if source_status:
        detail["source_subtitle"] = source_status

    output_status = _job_output_status(result)
    if output_status:
        detail["output"] = output_status
    return detail


def _job_translation_progress(progress: Optional[Dict[str, Any]], translation_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    progress_details = _dict_or_empty(progress.get("details") if progress else None)
    total_chunks = progress_details.get("total_chunks")
    completed_chunks = progress_details.get("completed_chunks")
    chunk_index = progress_details.get("chunk_index")
    summary = _dict_or_empty(translation_result.get("translation_summary"))
    if total_chunks is None:
        total_chunks = summary.get("chunks")
    if completed_chunks is None and summary.get("chunk_summaries") is not None:
        chunk_summaries = summary.get("chunk_summaries")
        if isinstance(chunk_summaries, list):
            completed_chunks = len(chunk_summaries)
    payload = {
        "chunk_index": chunk_index,
        "completed_chunks": completed_chunks,
        "total_chunks": total_chunks,
        "cue_count": progress_details.get("cue_count") or summary.get("cue_count"),
    }
    cleaned = {key: value for key, value in payload.items() if value is not None}
    return cleaned or None


def _job_source_status(milestones: Dict[str, Any], translation_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    source_acquisition = _dict_or_empty(translation_result.get("source_acquisition"))
    provider_fallback = _dict_or_empty(source_acquisition.get("provider_fallback"))
    provider_candidate = _dict_or_empty(provider_fallback.get("candidate"))
    if source_acquisition:
        payload: Dict[str, Any] = {
            "status": source_acquisition.get("status"),
            "method": source_acquisition.get("method"),
            "provider": provider_candidate.get("provider") or _dict_or_empty(provider_fallback.get("download")).get("provider"),
            "language": provider_candidate.get("language"),
            "file_name": provider_candidate.get("file_name"),
            "match": provider_fallback.get("match"),
        }
        return {key: value for key, value in payload.items() if value is not None}

    selected = _dict_or_empty(_dict_or_empty(milestones.get("online_subtitle_selected")).get("details"))
    if selected:
        return {
            "status": "ready",
            "method": "online_provider",
            **{key: value for key, value in selected.items() if key in {"provider", "language", "file_name", "source_language"}},
        }
    local = _dict_or_empty(_dict_or_empty(milestones.get("source_subtitle_ready")).get("details"))
    if local:
        return {
            "status": "ready",
            "method": local.get("method"),
            "source_language": local.get("source_language"),
        }
    missing = _dict_or_empty(_dict_or_empty(milestones.get("local_source_missing")).get("details"))
    if missing:
        return {
            "status": "missing_local_source",
            "method": missing.get("method"),
            "source_language": missing.get("source_language"),
            "reason": missing.get("reason"),
        }
    return None


def _job_output_status(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not result:
        return None
    write_back = _dict_or_empty(result.get("write_back"))
    output = {
        "staged_path": result.get("output"),
        "write_back_requested": write_back.get("requested"),
        "write_back_status": write_back.get("status"),
        "write_back_path": write_back.get("path"),
    }
    return {key: value for key, value in output.items() if value is not None}


def _default_job_status_message(status: str) -> str:
    if status == "queued":
        return "Subtitle job is queued."
    if status == "running":
        return "Subtitle job is processing."
    if status == "succeeded":
        return "Subtitle job completed."
    if status == "failed":
        return "Subtitle job failed."
    if status == "needs_confirmation":
        return "Subtitle job needs user confirmation."
    return "Subtitle job status is unknown."


def _dict_or_empty(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _dict_or_none(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    return None


def _job_kind(job: Dict[str, Any]) -> str:
    return str((job.get("request") or {}).get("kind") or "translate-plex")


def job_run_summary(args: argparse.Namespace, executor=None, allow_running: bool = False) -> Dict[str, Any]:
    store = JobStore(args.job_store_dir)
    if executor is not None:
        job_executor = executor
    else:
        job = store.get(args.job_id)
        job_executor = translate_video_job_executor if _job_kind(job) == "translate-video" else translate_plex_job_executor
    allow_low_confidence = bool(getattr(args, "allow_low_confidence_subtitle", False))
    allow_fallback_language = bool(getattr(args, "allow_provider_fallback_language", False))
    allow_confirmation = allow_low_confidence or allow_fallback_language
    if allow_low_confidence:
        store.confirm_low_confidence(args.job_id)
    if allow_fallback_language:
        store.confirm_provider_fallback_language(args.job_id)

    def progress_callback(progress):
        store.mark_progress(args.job_id, progress)

    setattr(args, "progress_callback", progress_callback)

    def wrapped(job):
        try:
            return job_executor(job, args)
        except LowConfidenceSubtitleCandidatesError as error:
            raise JobNeedsConfirmation(error.to_dict()) from error
        except ProviderFallbackLanguageError as error:
            raise JobNeedsConfirmation(error.to_dict()) from error

    try:
        job = run_job(
            store,
            args.job_id,
            wrapped,
            allow_needs_confirmation=allow_confirmation,
            allow_running=allow_running,
        )
    finally:
        with contextlib.suppress(Exception):
            from . import notifications

            with contextlib.suppress(Exception):
                notifications.touch_notification_wake_file()
            # Safety net: if the daemon idle-exited while this job ran, the
            # terminal notice would otherwise wait for the next job_start.
            watch_store_path = notifications.default_notification_watch_store_path()
            if watch_store_path.exists() and notifications.JobNotificationStore(watch_store_path).pending_watches():
                notifications.start_notification_daemon_from_env()
    _maybe_mirror_runtime_job_status(args, job)
    return {
        "job_store": str(store.root),
        "job": job,
    }


def job_start_summary(args: argparse.Namespace, popen=subprocess.Popen) -> Dict[str, Any]:
    store = JobStore(args.job_store_dir)
    logs_dir = store.root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / ("%s.out.log" % args.job_id)
    stderr_path = logs_dir / ("%s.err.log" % args.job_id)
    command = [
        sys.executable,
        "-m",
        "mpilot.subtitles",
        "job-run",
        "--job-store-dir",
        str(store.root),
    ]
    if args.allow_low_confidence_subtitle:
        command.append("--allow-low-confidence-subtitle")
    if getattr(args, "allow_provider_fallback_language", False):
        command.append("--allow-provider-fallback-language")
    command.append(args.job_id)
    env = _job_start_env(args)
    cwd = Path(__file__).resolve().parents[1]
    allow_any_confirmation = args.allow_low_confidence_subtitle or bool(getattr(args, "allow_provider_fallback_language", False))

    with store.lock(args.job_id):
        job = store.get(args.job_id)
        status = str(job.get("status") or "")
        if status == "running":
            summary = {
                "status": "already_running",
                "job_store": str(store.root),
                "job": job,
            }
            _attach_job_start_notification(summary, args)
            return summary
        if status == "succeeded":
            raise JobStoreError("job already succeeded: %s" % args.job_id)
        if status == "needs_confirmation" and not allow_any_confirmation:
            raise JobStoreError("job needs confirmation before retry: %s" % args.job_id)
        if status not in {"queued", "failed", "needs_confirmation"}:
            raise JobStoreError("job cannot start from status %s: %s" % (status, args.job_id))
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            process = popen(
                command,
                cwd=str(cwd),
                env=env,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
            )

    summary = {
        "status": "started",
        "job_store": str(store.root),
        "job": store.get(args.job_id),
        "worker": {
            "pid": process.pid,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        },
    }
    _attach_job_start_notification(summary, args)
    return summary


def _job_start_env(args: argparse.Namespace) -> Dict[str, str]:
    env = os.environ.copy()
    mapping = {
        "plex_base_url": ("PLEX_BASE_URL",),
        "plex_token": ("PLEX_TOKEN",),
        "plex_path_prefix": ("MPILOT_PLEX_PATH_PREFIX",),
        "local_path_prefix": ("MPILOT_LOCAL_PATH_PREFIX",),
        "openai_base_url": ("OPENAI_BASE_URL",),
        "openai_api_key": ("OPENAI_API_KEY",),
        "opensubtitles_api_key": ("OPENSUBTITLES_API_KEY",),
        "opensubtitles_user_agent": ("OPENSUBTITLES_USER_AGENT",),
        "opensubtitles_username": ("OPENSUBTITLES_USERNAME",),
        "opensubtitles_password": ("OPENSUBTITLES_PASSWORD",),
        "opensubtitles_token": ("OPENSUBTITLES_TOKEN",),
        "subdl_api_key": ("SUBDL_API_KEY",),
        "runtime_store_dir": ("MPILOT_RUNTIME_STORE_DIR",),
        "runtime_workflow_id": ("MPILOT_RUNTIME_WORKFLOW_ID",),
        "runtime_task_id": ("MPILOT_RUNTIME_TASK_ID",),
    }
    for attr, env_names in mapping.items():
        value = getattr(args, attr, None)
        if value is not None:
            for env_name in env_names:
                env[env_name] = str(value)
    for env_name in (
        "MPILOT_SUBTITLE_JOB_NOTIFICATION_TARGET",
        "BABELARR_JOB_NOTIFICATION_TARGET",
        "MST_JOB_NOTIFICATION_TARGET",
        "MPILOT_SUBTITLE_JOB_NOTIFICATION_REQUESTER_ID",
        "BABELARR_JOB_NOTIFICATION_REQUESTER_ID",
        "MST_JOB_NOTIFICATION_REQUESTER_ID",
        "MPILOT_SUBTITLE_JOB_NOTIFICATION_TITLE",
        "BABELARR_JOB_NOTIFICATION_TITLE",
        "MST_JOB_NOTIFICATION_TITLE",
        "MPILOT_SUBTITLE_JOB_NOTIFICATION_LANGUAGE",
        "BABELARR_JOB_NOTIFICATION_LANGUAGE",
        "MST_JOB_NOTIFICATION_LANGUAGE",
    ):
        env.pop(env_name, None)
    return env


def _attach_job_start_notification(summary: Dict[str, Any], args: argparse.Namespace) -> None:
    from . import notifications

    target = notifications.resolve_notification_target(
        getattr(args, "notification_target", None),
        getattr(args, "requester_id", None),
    )
    if not target:
        return
    status = summary.get("status")
    if status not in {"started", "already_running"}:
        return
    job = summary.get("job")
    if not isinstance(job, dict):
        return
    job_id = job.get("job_id")
    if not isinstance(job_id, str) or not job_id.strip():
        return
    job_store = summary.get("job_store")
    if not isinstance(job_store, str) or not job_store.strip():
        return

    notifier = notifications.JobCompletionNotifier.from_env()
    watch = notifier.register_watch(
        job_id=job_id,
        job_store_dir=job_store,
        notification_target=target,
        title=getattr(args, "notification_title", None) or _job_notification_title(job),
        requester_id=getattr(args, "requester_id", None),
        language=getattr(args, "notification_language", None) or _job_target_language(job),
        metadata=_job_notification_metadata(job),
        initial_notification_delay_seconds=notifications.initial_notification_delay_seconds_from_env(),
    )
    with contextlib.suppress(Exception):
        notifications.touch_notification_wake_file()
    with contextlib.suppress(Exception):
        notifications.start_notification_daemon_from_env()
    summary["notification_watch"] = {
        "status": "watching",
        "watch": watch,
    }


def _job_notification_title(job: Dict[str, Any]) -> str:
    request = job.get("request")
    if not isinstance(request, dict):
        return "subtitle job"
    video = request.get("video")
    if isinstance(video, dict):
        title = video.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
        video_path = video.get("video_path")
        if isinstance(video_path, str) and video_path.strip():
            return Path(video_path).name
        imdb_id = video.get("imdb_id")
        if isinstance(imdb_id, str) and imdb_id.strip():
            return imdb_id.strip()
    plex = request.get("plex")
    if isinstance(plex, dict):
        imdb = plex.get("imdb")
        if isinstance(imdb, str) and imdb.strip():
            return imdb.strip()
        rating_key = plex.get("rating_key")
        if isinstance(rating_key, str) and rating_key.strip():
            return "Plex item %s" % rating_key.strip()
    return "subtitle job"


def _job_target_language(job: Dict[str, Any]) -> Optional[str]:
    request = job.get("request")
    if not isinstance(request, dict):
        return None
    translation = request.get("translation")
    if not isinstance(translation, dict):
        return None
    target_language = translation.get("target_language")
    return target_language if isinstance(target_language, str) else None


def _job_notification_metadata(job: Dict[str, Any]) -> Dict[str, str]:
    metadata: Dict[str, str] = {}
    request = job.get("request")
    if not isinstance(request, dict):
        return metadata
    plex = request.get("plex")
    if isinstance(plex, dict):
        imdb = plex.get("imdb")
        rating_key = plex.get("rating_key")
        if isinstance(imdb, str) and imdb.strip():
            metadata["imdb"] = imdb.strip()
        if isinstance(rating_key, str) and rating_key.strip():
            metadata["rating_key"] = rating_key.strip()
    video = request.get("video")
    if isinstance(video, dict):
        imdb_id = video.get("imdb_id")
        title = video.get("title")
        media_type = video.get("media_type")
        if isinstance(imdb_id, str) and imdb_id.strip():
            metadata["imdb"] = imdb_id.strip()
        if isinstance(title, str) and title.strip():
            metadata["title"] = title.strip()
        if isinstance(media_type, str) and media_type.strip():
            metadata["media_type"] = media_type.strip()
    return metadata


def _env_string(name: str) -> Optional[str]:
    value = _env_first(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _maybe_mirror_runtime_job_status(args: argparse.Namespace, job: Dict[str, Any]) -> None:
    runtime_store_dir = getattr(args, "runtime_store_dir", None) or _env_first("BABELARR_RUNTIME_STORE_DIR", "MST_RUNTIME_STORE_DIR")
    workflow_id = getattr(args, "runtime_workflow_id", None) or _env_first("BABELARR_RUNTIME_WORKFLOW_ID", "MST_RUNTIME_WORKFLOW_ID")
    task_id = getattr(args, "runtime_task_id", None) or _env_first("BABELARR_RUNTIME_TASK_ID", "MST_RUNTIME_TASK_ID")
    if not runtime_store_dir or not workflow_id or not task_id:
        return
    status = str(job.get("status") or "")
    if status not in {"queued", "running", "succeeded", "failed", "needs_confirmation"}:
        return
    with contextlib.suppress(Exception):
        from mpilot.runtime import MediaWorkflowRuntime

        runtime = MediaWorkflowRuntime(Path(runtime_store_dir))
        last_error = job.get("last_error")
        runtime.record_mst_job_status(
            workflow_id=workflow_id,
            task_id=task_id,
            status=status,
            status_detail=job_status_detail(job),
            result=job.get("result") if isinstance(job.get("result"), dict) else None,
            error=last_error if isinstance(last_error, dict) else None,
        )
        _maybe_dispatch_next_runtime_subtitle_task(args, runtime, status)


def _maybe_dispatch_next_runtime_subtitle_task(args: argparse.Namespace, runtime: Any, status: str) -> None:
    if status not in {"succeeded", "failed", "needs_confirmation"}:
        return
    with contextlib.suppress(Exception):
        from mpilot.runtime import dispatcher as runtime_dispatcher

        runtime_dispatcher.dispatch_ready_mst_actions(
            runtime,
            job_store_dir=str(args.job_store_dir),
            openai_base_url=getattr(args, "openai_base_url", None),
            openai_api_key=getattr(args, "openai_api_key", None),
            opensubtitles_api_key=getattr(args, "opensubtitles_api_key", None),
            opensubtitles_user_agent=getattr(args, "opensubtitles_user_agent", None),
            opensubtitles_username=getattr(args, "opensubtitles_username", None),
            opensubtitles_password=getattr(args, "opensubtitles_password", None),
            opensubtitles_token=getattr(args, "opensubtitles_token", None),
            subdl_api_key=getattr(args, "subdl_api_key", None),
        )


def _add_runtime_mirror_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-store-dir", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-workflow-id", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-task-id", help=argparse.SUPPRESS)


def job_resume_summary(args: argparse.Namespace, executor=None) -> Dict[str, Any]:
    if getattr(args, "allow_low_confidence_subtitle", False):
        raise ValueError("job-resume cannot bulk-confirm low-confidence subtitles; run job-run for the specific job")
    store = JobStore(args.job_store_dir)
    stale_after = timedelta(seconds=args.stale_after_seconds)
    jobs = store.recoverable_jobs(datetime.now(timezone.utc), stale_after)[: args.limit]
    results = []
    for job in jobs:
        run_args = argparse.Namespace(**vars(args))
        run_args.job_id = job["job_id"]
        run_args.allow_low_confidence_subtitle = False
        try:
            results.append(job_run_summary(run_args, executor=executor, allow_running=True)["job"])
        except JobStoreError as error:
            results.append(
                {
                    "job_id": job["job_id"],
                    "status": "skipped",
                    "reason": str(error),
                }
            )
    return {
        "job_store": str(store.root),
        "count": len(results),
        "jobs": results,
    }


def job_prune_summary(args: argparse.Namespace, now: Optional[datetime] = None) -> Dict[str, Any]:
    if args.retention_days < 0:
        raise ValueError("retention-days must be non-negative")
    store = JobStore(args.job_store_dir)
    retention = timedelta(days=args.retention_days)
    summary = store.prune(
        now=now or datetime.now(timezone.utc),
        retention=retention,
        dry_run=args.dry_run,
    )
    summary["job_store"] = str(store.root)
    summary["retention_days"] = args.retention_days
    return summary


def notify_daemon_summary(args: argparse.Namespace) -> Dict[str, Any]:
    from . import notifications

    return notifications.run_notification_daemon(
        idle_exit_seconds=args.idle_exit_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        run_once=bool(getattr(args, "once", False)),
        lock_acquire_timeout_seconds=getattr(args, "lock_acquire_timeout_seconds", 0.0),
    )


def job_request_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "kind": "translate-plex",
        "plex": {
            "rating_key": args.rating_key,
            "imdb": args.imdb,
            "season": args.season,
            "episode": args.episode,
            "plex_base_url": args.plex_base_url,
            "plex_path_prefix": args.plex_path_prefix,
            "local_path_prefix": args.local_path_prefix,
        },
        "translation": {
            "source_language": args.source_language,
            "target_language": args.target_language,
            "output_mode": args.output_mode,
            "backend": args.backend,
            "model": args.model,
            "assume_unlabeled_stream_language": args.assume_unlabeled_stream_language,
        },
        "render": {
            "primary_script": args.primary_script,
            "secondary_script": args.secondary_script,
            "ass_height": args.ass_height,
        },
        "output": {
            "output": _optional_path_string(args.output),
            "force": args.force,
            "work_dir": _optional_path_string(args.work_dir),
            "keep_work_dir": args.keep_work_dir,
            "write_back": args.write_back,
            "refresh_plex": args.refresh_plex,
        },
        "provider": {
            "online_subtitle_fallback": not args.no_online_subtitle_fallback,
            "subtitle_provider": args.subtitle_provider,
            "provider_search_limit": args.provider_search_limit,
            "download_provider_priority": args.download_provider_priority,
            "allow_low_confidence_subtitle": args.allow_low_confidence_subtitle,
            "allow_provider_fallback_language": bool(getattr(args, "allow_provider_fallback_language", False)),
        },
    }


def translate_plex_job_executor(job: Dict[str, Any], runtime_args: argparse.Namespace) -> Dict[str, Any]:
    args = translate_plex_args_from_job(job, runtime_args)
    progress_callback = getattr(runtime_args, "progress_callback", None)
    if progress_callback is not None:
        setattr(args, "progress_callback", progress_callback)
    _emit_args_progress(
        args,
        "resolving_media",
        "Resolving Plex item.",
        imdb=args.imdb,
        rating_key=args.rating_key,
        season=args.season,
        episode=args.episode,
    )
    connection = PlexConnection.from_values(args.plex_base_url, args.plex_token)
    path_mapping = PathMapping.from_values(args.plex_path_prefix, args.local_path_prefix)
    plex_client = PlexApiClient(connection)
    resolver = PlexResolver(plex_client, path_mapping)
    resolved = resolver.resolve(
        imdb=args.imdb,
        rating_key=args.rating_key,
        season=args.season,
        episode=args.episode,
    )
    _emit_args_progress(
        args,
        "media_resolved",
        "Resolved Plex item to a media file.",
        title=resolved.title,
        media_type=resolved.media_type,
        local_file=resolved.local_file,
    )
    return translate_plex_resolved(resolved, options_from_args(args, plex_client=plex_client))


def translate_plex_args_from_job(job: Dict[str, Any], runtime_args: argparse.Namespace) -> argparse.Namespace:
    request = job.get("request") or {}
    plex = request.get("plex") or {}
    translation = request.get("translation") or {}
    render = request.get("render") or {}
    output = request.get("output") or {}
    provider = request.get("provider") or {}
    return argparse.Namespace(
        command="translate-plex",
        imdb=plex.get("imdb"),
        rating_key=plex.get("rating_key"),
        season=plex.get("season"),
        episode=plex.get("episode"),
        plex_base_url=getattr(runtime_args, "plex_base_url", None) or plex.get("plex_base_url"),
        plex_token=getattr(runtime_args, "plex_token", None),
        plex_path_prefix=getattr(runtime_args, "plex_path_prefix", None) or plex.get("plex_path_prefix"),
        local_path_prefix=getattr(runtime_args, "local_path_prefix", None) or plex.get("local_path_prefix"),
        source_language=translation.get("source_language", "en"),
        target_language=translation.get("target_language", "zh"),
        output_mode=translation.get("output_mode", "bilingual-ass"),
        backend=translation.get("backend", "codex-cli"),
        model=translation.get("model", "gpt-5.4-mini"),
        assume_unlabeled_stream_language=bool(translation.get("assume_unlabeled_stream_language", False)),
        output=_optional_path(output.get("output")),
        force=bool(output.get("force", False)),
        work_dir=_optional_path(output.get("work_dir")),
        keep_work_dir=bool(output.get("keep_work_dir", False)),
        primary_script=render.get("primary_script", "cjk"),
        secondary_script=render.get("secondary_script", "latin"),
        ass_height=int(render.get("ass_height", 1080)),
        openai_base_url=getattr(runtime_args, "openai_base_url", "http://127.0.0.1:11434/v1"),
        openai_api_key=getattr(runtime_args, "openai_api_key", "ollama"),
        no_online_subtitle_fallback=not bool(provider.get("online_subtitle_fallback", True)),
        write_back=bool(output.get("write_back", False)),
        refresh_plex=bool(output.get("refresh_plex", False)),
        allow_low_confidence_subtitle=bool(provider.get("allow_low_confidence_subtitle", False))
        or bool(getattr(runtime_args, "allow_low_confidence_subtitle", False)),
        allow_provider_fallback_language=bool(provider.get("allow_provider_fallback_language", False))
        or bool(getattr(runtime_args, "allow_provider_fallback_language", False)),
        subtitle_provider=provider.get("subtitle_provider", "all"),
        provider_search_limit=int(provider.get("provider_search_limit", 10)),
        download_provider_priority=provider.get("download_provider_priority", ",".join(DEFAULT_DOWNLOAD_PROVIDER_PRIORITY)),
        opensubtitles_api_key=getattr(runtime_args, "opensubtitles_api_key", None),
        opensubtitles_user_agent=getattr(runtime_args, "opensubtitles_user_agent", None),
        opensubtitles_username=getattr(runtime_args, "opensubtitles_username", None),
        opensubtitles_password=getattr(runtime_args, "opensubtitles_password", None),
        opensubtitles_token=getattr(runtime_args, "opensubtitles_token", None),
        subdl_api_key=getattr(runtime_args, "subdl_api_key", None),
    )


def job_create_video_summary(args: argparse.Namespace) -> Dict[str, Any]:
    store = JobStore(args.job_store_dir)
    job = store.create("translate-video", video_job_request_from_args(args))
    return {
        "job_store": str(store.root),
        "job": job,
    }


def video_job_request_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    _validate_episode_context(getattr(args, "season", None), getattr(args, "episode", None))
    video: Dict[str, Any] = {
        "video_path": str(args.video_path),
        "imdb_id": getattr(args, "imdb_id", None),
        "title": getattr(args, "title", None),
        "media_type": getattr(args, "media_type", "movie"),
    }
    if getattr(args, "season", None) is not None:
        video["season"] = args.season
    if getattr(args, "episode", None) is not None:
        video["episode"] = args.episode
    return {
        "kind": "translate-video",
        "video": video,
        "translation": {
            "source_language": args.source_language,
            "target_language": args.target_language,
            "output_mode": args.output_mode,
            "backend": args.backend,
            "model": args.model,
            "assume_unlabeled_stream_language": args.assume_unlabeled_stream_language,
        },
        "render": {
            "primary_script": args.primary_script,
            "secondary_script": args.secondary_script,
            "ass_height": args.ass_height,
        },
        "output": {
            "output": _optional_path_string(args.output),
            "force": args.force,
            "work_dir": _optional_path_string(args.work_dir),
            "keep_work_dir": args.keep_work_dir,
        },
        "provider": {
            "online_subtitle_fallback": not args.no_online_subtitle_fallback,
            "subtitle_provider": args.subtitle_provider,
            "provider_search_limit": args.provider_search_limit,
            "download_provider_priority": args.download_provider_priority,
            "allow_low_confidence_subtitle": args.allow_low_confidence_subtitle,
            "allow_provider_fallback_language": bool(getattr(args, "allow_provider_fallback_language", False)),
        },
    }


def translate_video_job_executor(job: Dict[str, Any], runtime_args: argparse.Namespace) -> Dict[str, Any]:
    import types
    args = translate_video_args_from_job(job, runtime_args)
    progress_callback = getattr(runtime_args, "progress_callback", None)
    if progress_callback is not None:
        setattr(args, "progress_callback", progress_callback)
    video_path = Path(args.video_path)
    _emit_args_progress(
        args,
        "starting",
        "Preparing to translate video file.",
        input=str(video_path),
    )
    resolved_media = None
    if args.imdb_id or args.title:
        season, episode = _episode_context_for_video_job(args.media_type, args.title, video_path, args.season, args.episode)
        resolved_media = types.SimpleNamespace(
            media_type=args.media_type or "movie",
            title=args.title or video_path.stem,
            show_title=None,
            imdb=args.imdb_id,
            season=season,
            episode=episode,
            local_file=str(video_path),
        )
    return translate_video_file(video_path, options_from_args(args), resolved_media=resolved_media)


def translate_video_args_from_job(job: Dict[str, Any], runtime_args: argparse.Namespace) -> argparse.Namespace:
    request = job.get("request") or {}
    video = request.get("video") or {}
    translation = request.get("translation") or {}
    render = request.get("render") or {}
    output = request.get("output") or {}
    provider = request.get("provider") or {}
    return argparse.Namespace(
        command="translate-video",
        video_path=video.get("video_path"),
        imdb_id=video.get("imdb_id"),
        title=video.get("title"),
        media_type=video.get("media_type", "movie"),
        season=video.get("season"),
        episode=video.get("episode"),
        source_language=translation.get("source_language", "en"),
        target_language=translation.get("target_language", "zh"),
        output_mode=translation.get("output_mode", "bilingual-ass"),
        backend=translation.get("backend", "codex-cli"),
        model=translation.get("model", "gpt-5.4-mini"),
        assume_unlabeled_stream_language=bool(translation.get("assume_unlabeled_stream_language", False)),
        output=_optional_path(output.get("output")),
        force=bool(output.get("force", False)),
        work_dir=_optional_path(output.get("work_dir")),
        keep_work_dir=bool(output.get("keep_work_dir", False)),
        primary_script=render.get("primary_script", "cjk"),
        secondary_script=render.get("secondary_script", "latin"),
        ass_height=int(render.get("ass_height", 1080)),
        openai_base_url=getattr(runtime_args, "openai_base_url", "http://127.0.0.1:11434/v1"),
        openai_api_key=getattr(runtime_args, "openai_api_key", "ollama"),
        no_online_subtitle_fallback=not bool(provider.get("online_subtitle_fallback", True)),
        allow_low_confidence_subtitle=bool(provider.get("allow_low_confidence_subtitle", False))
        or bool(getattr(runtime_args, "allow_low_confidence_subtitle", False)),
        allow_provider_fallback_language=bool(provider.get("allow_provider_fallback_language", False))
        or bool(getattr(runtime_args, "allow_provider_fallback_language", False)),
        subtitle_provider=provider.get("subtitle_provider", "all"),
        provider_search_limit=int(provider.get("provider_search_limit", 10)),
        download_provider_priority=provider.get("download_provider_priority", ",".join(DEFAULT_DOWNLOAD_PROVIDER_PRIORITY)),
        opensubtitles_api_key=getattr(runtime_args, "opensubtitles_api_key", None),
        opensubtitles_user_agent=getattr(runtime_args, "opensubtitles_user_agent", None),
        opensubtitles_username=getattr(runtime_args, "opensubtitles_username", None),
        opensubtitles_password=getattr(runtime_args, "opensubtitles_password", None),
        opensubtitles_token=getattr(runtime_args, "opensubtitles_token", None),
        subdl_api_key=getattr(runtime_args, "subdl_api_key", None),
    )


def _validate_episode_context(season: Optional[int], episode: Optional[int]) -> None:
    if (season is None) != (episode is None):
        raise ValueError("pass both --season and --episode for TV episodes")


def _episode_context_for_video_job(
    media_type: Optional[str],
    title: Optional[str],
    video_path: Path,
    season: Optional[int],
    episode: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    if season is not None or episode is not None:
        return season, episode
    if str(media_type or "").lower() not in {"episode", "tv"}:
        return None, None
    for value in (video_path.name, title or ""):
        parsed = parse_release_info(value)
        if parsed.season is not None and parsed.episode is not None:
            return parsed.season, parsed.episode
    return None, None


def _optional_path_string(path: Optional[Path]) -> Optional[str]:
    return str(path) if path is not None else None


def _optional_path(value: Optional[str]) -> Optional[Path]:
    return Path(value) if value else None


def build_subtitle_provider(provider_name: str, args: argparse.Namespace):
    if provider_name == "opensubtitles":
        return OpenSubtitlesProvider(
            OpenSubtitlesConfig.from_values(
                api_key=args.opensubtitles_api_key,
                user_agent=args.opensubtitles_user_agent,
                username=args.opensubtitles_username,
                password=args.opensubtitles_password,
                token=args.opensubtitles_token,
            )
        )
    if provider_name == "subdl":
        return SubDLProvider(SubDLConfig.from_values(api_key=args.subdl_api_key))
    raise ValueError("unknown subtitle provider: %s" % provider_name)


def cli_error_summary(error: BaseException, command: Optional[str] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "error": {
            "type": error.__class__.__name__,
            "message": str(error),
        }
    }
    if command:
        payload["error"]["command"] = command
    return payload


def summary_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "translate-srt":
        return translate_srt_file(args.input, options_from_args(args))
    if args.command == "translate-video":
        return translate_video_file(args.input, options_from_args(args))
    if args.command == "plex-resolve":
        connection = PlexConnection.from_values(args.plex_base_url, args.plex_token)
        path_mapping = PathMapping.from_values(args.plex_path_prefix, args.local_path_prefix)
        resolver = PlexResolver(PlexApiClient(connection), path_mapping)
        return resolver.resolve(
            imdb=args.imdb,
            rating_key=args.rating_key,
            season=args.season,
            episode=args.episode,
        ).to_dict()
    if args.command == "plex-search":
        return plex_search_summary(args)
    if args.command == "subtitle-plan":
        connection = PlexConnection.from_values(args.plex_base_url, args.plex_token)
        path_mapping = PathMapping.from_values(args.plex_path_prefix, args.local_path_prefix)
        resolver = PlexResolver(PlexApiClient(connection), path_mapping)
        resolved = resolver.resolve(
            imdb=args.imdb,
            rating_key=args.rating_key,
            season=args.season,
            episode=args.episode,
        )
        return build_subtitle_plan(
            resolved,
            target_language=args.target_language,
            preferred_source_language=args.preferred_source_language,
        )
    if args.command == "subtitle-search":
        return subtitle_search_summary(args)
    if args.command == "subtitle-fetch":
        return subtitle_fetch_summary(args)
    if args.command == "subtitle-download":
        return subtitle_download_summary(args)
    if args.command == "translate-plex":
        connection = PlexConnection.from_values(args.plex_base_url, args.plex_token)
        path_mapping = PathMapping.from_values(args.plex_path_prefix, args.local_path_prefix)
        plex_client = PlexApiClient(connection)
        resolver = PlexResolver(plex_client, path_mapping)
        resolved = resolver.resolve(
            imdb=args.imdb,
            rating_key=args.rating_key,
            season=args.season,
            episode=args.episode,
        )
        return translate_plex_resolved(resolved, options_from_args(args, plex_client=plex_client))
    if args.command == "translate-plex-season":
        connection = PlexConnection.from_values(args.plex_base_url, args.plex_token)
        path_mapping = PathMapping.from_values(args.plex_path_prefix, args.local_path_prefix)
        plex_client = PlexApiClient(connection)
        resolver = PlexResolver(plex_client, path_mapping)
        return translate_plex_season(
            resolver,
            imdb=args.imdb,
            rating_key=args.rating_key,
            season=args.season,
            episode_start=args.episode_start,
            episode_end=args.episode_end,
            batch_size=args.batch_size,
            options=options_from_args(args, plex_client=plex_client),
        )
    if args.command == "job-create":
        return job_create_summary(args)
    if args.command == "job-create-video":
        return job_create_video_summary(args)
    if args.command == "job-show":
        return job_show_summary(args)
    if args.command == "job-list":
        return job_list_summary(args)
    if args.command == "job-run":
        return job_run_summary(args)
    if args.command == "job-start":
        return job_start_summary(args)
    if args.command == "job-resume":
        return job_resume_summary(args)
    if args.command == "job-prune":
        return job_prune_summary(args)
    if args.command == "notify-daemon":
        return notify_daemon_summary(args)
    raise ValueError("unknown command: %s" % args.command)


def plex_search_summary(args: argparse.Namespace) -> Dict[str, Any]:
    local_root = _local_search_root(args)
    if local_root is not None and args.season is not None and args.episode is not None:
        local_result = local_search_by_title(
            args.query,
            local_root,
            season=args.season,
            episode=args.episode,
            year=args.year,
            limit=args.limit,
        )
        if local_result.get("status") != "no_match":
            local_result["fallback_reason"] = "local_tv_episode_match"
            return local_result

    plex_error: Optional[PlexResolverError] = None
    try:
        connection = PlexConnection.from_values(args.plex_base_url, args.plex_token)
        path_mapping = PathMapping.from_values(args.plex_path_prefix, args.local_path_prefix)
        resolver = PlexResolver(PlexApiClient(connection), path_mapping)
        result = resolver.search_by_title(
            args.query,
            season=args.season,
            episode=args.episode,
            year=args.year,
            limit=args.limit,
        )
        if result.get("status") != "no_match":
            return result
    except PlexResolverError as error:
        plex_error = error

    if local_root is not None:
        local_result = local_search_by_title(
            args.query,
            local_root,
            season=args.season,
            episode=args.episode,
            year=args.year,
            limit=args.limit,
        )
        if local_result.get("status") != "no_match" or plex_error is not None:
            if plex_error is not None:
                local_result["fallback_reason"] = str(plex_error)
            else:
                local_result["fallback_reason"] = "plex_no_match"
            return local_result
    if plex_error is not None:
        raise plex_error
    return result


def _local_search_root(args: argparse.Namespace) -> Optional[Path]:
    value = (
        getattr(args, "local_path_prefix", None)
        or _env_first("BABELARR_LOCAL_PATH_PREFIX", "MST_LOCAL_PATH_PREFIX")
    )
    return Path(value) if value else None


def main(argv=None, *, prog: str = "mpilot subtitles") -> int:
    parser = build_parser(prog=prog)
    args = parser.parse_args(argv)
    try:
        summary = summary_from_args(args)
        print(summary_json(summary))
        return 0
    except LowConfidenceSubtitleCandidatesError as error:
        print(
            summary_json(
                {
                    "status": "needs_confirmation",
                    "proposal": error.to_dict(),
                    **cli_error_summary(error, getattr(args, "command", None)),
                }
            )
        )
        return 2
    except Exception as error:
        print(summary_json(cli_error_summary(error, getattr(args, "command", None))))
        print("mpilot subtitles: %s" % error, file=sys.stderr)
        return 1
