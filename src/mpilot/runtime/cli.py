from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import MediaWorkflowRuntime, RuntimeStoreError
from .dispatcher import dispatch_qbitlarr_completion, dispatch_ready_babelarr_actions, reconcile_terminal_babelarr_jobs


def default_runtime_store_dir() -> Path:
    configured = (
        os.environ.get("MPILOT_RUNTIME_STORE_DIR")
        or os.environ.get("BABELARR_RUNTIME_STORE_DIR")
        or os.environ.get("MWR_STORE_DIR")
        or os.environ.get("MEDIA_WORKFLOW_RUNTIME_STORE_DIR")
    )
    if configured:
        return Path(configured).expanduser()
    default_path = Path.home() / ".local" / "share" / "mpilot" / "runtime" / "workflows"
    legacy_dir = "media-" + "workflow-runtime"
    legacy_path = Path.home() / ".local" / "share" / legacy_dir / "workflows"
    if legacy_path.exists() and not default_path.exists():
        return legacy_path
    return default_path


def build_parser(prog: str = "mpilot runtime") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Coordinate cross-MCP media acquisition and subtitle workflow state.",
    )
    parser.add_argument(
        "--runtime-store-dir",
        type=Path,
        default=default_runtime_store_dir(),
        help=(
            "Runtime workflow store directory. Defaults to MPILOT_RUNTIME_STORE_DIR, "
            "BABELARR_RUNTIME_STORE_DIR, MWR_STORE_DIR, MEDIA_WORKFLOW_RUNTIME_STORE_DIR, or "
            "~/.local/share/mpilot/runtime/workflows."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_download = subparsers.add_parser(
        "record-acquisition-download",
        help="Create or update a media acquisition download task.",
    )
    _add_runtime_store_option(record_download)
    record_download.add_argument("--requester-id", required=True)
    record_download.add_argument("--info-hash", required=True)
    record_download.add_argument("--title")
    record_download.add_argument("--imdb-id")
    record_download.add_argument("--media-type")
    record_download.add_argument("--progress", type=float, default=0.0)
    record_download.add_argument("--content-path")
    _add_content_path_mapping_options(record_download)

    record_with_intent = subparsers.add_parser(
        "record-acquisition-download-with-subtitle-intent",
        help="Create or update a media acquisition download task and attach subtitle intent.",
    )
    _add_runtime_store_option(record_with_intent)
    record_with_intent.add_argument("--requester-id", required=True)
    record_with_intent.add_argument("--info-hash", required=True)
    record_with_intent.add_argument("--title")
    record_with_intent.add_argument("--imdb-id")
    record_with_intent.add_argument("--media-type")
    record_with_intent.add_argument("--progress", type=float, default=0.0)
    record_with_intent.add_argument("--content-path")
    record_with_intent.add_argument("--source-language", required=True)
    record_with_intent.add_argument("--target-language", required=True)
    record_with_intent.add_argument("--output-mode", required=True, choices=["single-srt", "bilingual-ass"])
    record_with_intent.add_argument("--notification-language")
    _add_content_path_mapping_options(record_with_intent)

    attach = subparsers.add_parser("attach-subtitle-intent", help="Attach subtitle intent to one active download.")
    _add_runtime_store_option(attach)
    attach.add_argument("--requester-id", required=True)
    attach.add_argument("--source-language", required=True)
    attach.add_argument("--target-language", required=True)
    attach.add_argument("--output-mode", required=True, choices=["single-srt", "bilingual-ass"])
    attach.add_argument("--notification-language")

    local_video = subparsers.add_parser(
        "record-local-video-subtitle-intent",
        help="Record a ready subtitle task for an already local or Plex-resolved video file.",
    )
    _add_runtime_store_option(local_video)
    local_video.add_argument("--requester-id", required=True)
    local_video.add_argument("--video-path", required=True)
    local_video.add_argument("--title")
    local_video.add_argument("--imdb-id")
    local_video.add_argument("--media-type")
    local_video.add_argument("--source-language", required=True)
    local_video.add_argument("--target-language", required=True)
    local_video.add_argument("--output-mode", required=True, choices=["single-srt", "bilingual-ass"])
    local_video.add_argument("--notification-language")
    _add_content_path_mapping_options(local_video)

    claim = subparsers.add_parser(
        "claim-ready-subtitle-job-actions",
        help="Claim ready subtitle direct-video actions.",
    )
    _add_runtime_store_option(claim)
    claim.add_argument("--limit", type=int)
    claim.add_argument("--workflow-id")

    dispatch = subparsers.add_parser(
        "dispatch-ready-subtitle-job-actions",
        help="Claim ready subtitle direct-video actions and start subtitle jobs.",
    )
    _add_runtime_store_option(dispatch)
    dispatch.add_argument("--workflow-id")
    _add_dispatch_options(dispatch)

    complete = subparsers.add_parser(
        "handle-acquisition-completion",
        help="Handle an acquisition completion event, release Runtime dependencies, and dispatch subtitle jobs.",
    )
    _add_runtime_store_option(complete)
    complete.add_argument("--info-hash")
    complete.add_argument("--content-path")
    complete.add_argument("--event-json", help="Completion event JSON. If omitted and required args are absent, stdin is read.")
    _add_dispatch_options(complete)

    created = subparsers.add_parser("record-subtitle-job-created", help="Record the subtitle job ID for a dispatched task.")
    _add_runtime_store_option(created)
    created.add_argument("--workflow-id", required=True)
    created.add_argument("--task-id", required=True)
    created.add_argument("--subtitle-job-id", required=True)

    status = subparsers.add_parser("record-subtitle-job-status", help="Mirror downstream subtitle job status into Runtime.")
    _add_runtime_store_option(status)
    status.add_argument("--workflow-id", required=True)
    status.add_argument("--task-id", required=True)
    status.add_argument("--status", required=True, choices=["queued", "running", "succeeded", "failed", "needs_confirmation"])
    status.add_argument("--status-detail-json")
    status.add_argument("--result-json")
    status.add_argument("--error-json")

    show = subparsers.add_parser("workflow-show", help="Show one Runtime workflow.")
    _add_runtime_store_option(show)
    show.add_argument("--workflow-id", required=True)

    workflow_list = subparsers.add_parser("workflow-list", help="List Runtime workflows.")
    _add_runtime_store_option(workflow_list)

    queue_status = subparsers.add_parser("queue-status", help="Show the global subtitle queue status.")
    _add_runtime_store_option(queue_status)
    queue_status.add_argument("--requester-id")
    queue_status.add_argument("--job-store-dir")

    return parser


def summary_from_argv(argv: Sequence[str], *, prog: str = "mpilot runtime") -> Dict[str, Any]:
    try:
        parser = build_parser(prog=prog)
        args = parser.parse_args(list(argv))
        runtime = MediaWorkflowRuntime(args.runtime_store_dir)
        return _summary_from_args(runtime, args)
    except RuntimeStoreError as error:
        return _error_summary(error)
    except json.JSONDecodeError as error:
        return {
            "status": "error",
            "error": {
                "type": "JSONDecodeError",
                "message": str(error),
            },
        }
    except SystemExit as error:
        if error.code == 0:
            raise
        return {
            "status": "error",
            "error": {
                "type": "ArgumentError",
                "message": "invalid arguments",
                "exit_code": error.code,
            },
        }


def main(argv: Optional[Sequence[str]] = None, *, prog: str = "mpilot runtime") -> int:
    payload = summary_from_argv(sys.argv[1:] if argv is None else argv, prog=prog)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("status") != "error" else 1


def _summary_from_args(runtime: MediaWorkflowRuntime, args: argparse.Namespace) -> Dict[str, Any]:
    if args.command == "record-acquisition-download":
        workflow = runtime.record_qbitlarr_download(
            requester_id=args.requester_id,
            info_hash=args.info_hash,
            title=args.title,
            imdb_id=args.imdb_id,
            media_type=args.media_type,
            progress=args.progress,
            content_path=_mapped_content_path(
                args.content_path,
                content_path_prefix=args.content_path_prefix,
                local_content_path_prefix=args.local_content_path_prefix,
            ),
        )
        return _workflow_payload(runtime, workflow)

    if args.command == "record-acquisition-download-with-subtitle-intent":
        workflow = runtime.record_qbitlarr_download_with_subtitle_intent(
            requester_id=args.requester_id,
            info_hash=args.info_hash,
            title=args.title,
            imdb_id=args.imdb_id,
            media_type=args.media_type,
            progress=args.progress,
            content_path=_mapped_content_path(
                args.content_path,
                content_path_prefix=args.content_path_prefix,
                local_content_path_prefix=args.local_content_path_prefix,
            ),
            source_language=args.source_language,
            target_language=args.target_language,
            output_mode=args.output_mode,
            notification_language=args.notification_language,
        )
        return _workflow_payload(runtime, workflow)

    if args.command == "attach-subtitle-intent":
        workflow = runtime.attach_subtitle_intent_to_current_download(
            requester_id=args.requester_id,
            source_language=args.source_language,
            target_language=args.target_language,
            output_mode=args.output_mode,
            notification_language=args.notification_language,
        )
        return _workflow_payload(runtime, workflow)

    if args.command == "record-local-video-subtitle-intent":
        workflow = runtime.record_local_video_subtitle_intent(
            requester_id=args.requester_id,
            video_path=_mapped_content_path(
                args.video_path,
                content_path_prefix=args.content_path_prefix,
                local_content_path_prefix=args.local_content_path_prefix,
            ),
            title=args.title,
            imdb_id=args.imdb_id,
            media_type=args.media_type,
            source_language=args.source_language,
            target_language=args.target_language,
            output_mode=args.output_mode,
            notification_language=args.notification_language,
        )
        return _workflow_payload(runtime, workflow)

    if args.command == "claim-ready-subtitle-job-actions":
        actions = runtime.claim_ready_babelarr_job_create_video_actions(limit=args.limit, workflow_id=args.workflow_id)
        return {
            "status": "ok",
            "runtime_store": str(runtime.root),
            "actions": actions,
        }

    if args.command == "dispatch-ready-subtitle-job-actions":
        summary = dispatch_ready_babelarr_actions(
            runtime,
            workflow_id=args.workflow_id,
            **_dispatcher_kwargs(args),
        )
        return {"runtime_store": str(runtime.root), **summary}

    if args.command == "handle-acquisition-completion":
        event = _completion_event_from_args(args)
        event_name = _event_string(event, "event")
        if event_name in {"download_removed", "download_deleted", "download_abandoned"}:
            workflow_clear = runtime.clear_qbitlarr_download_workflow(
                info_hash=args.info_hash or _event_string(event, "info_hash") or _event_string(event, "hash"),
                reason=event_name,
                error=_event_error(event),
            )
            return {
                "status": "ok",
                "runtime_store": str(runtime.root),
                "event": event_name,
                "workflow_clear": workflow_clear,
            }

        content_path = _mapped_content_path(
            args.content_path
            or _event_string(event, "content_path")
            or _event_string(event, "video_path")
            or _nested_event_string(event, "download_status", "content_path"),
            content_path_prefix=args.content_path_prefix,
            local_content_path_prefix=args.local_content_path_prefix,
        )
        summary = dispatch_qbitlarr_completion(
            runtime,
            info_hash=args.info_hash or _event_string(event, "info_hash") or _event_string(event, "hash"),
            content_path=content_path,
            **_dispatcher_kwargs(args, event=event),
        )
        return {"runtime_store": str(runtime.root), **summary}

    if args.command == "record-subtitle-job-created":
        workflow = runtime.record_babelarr_job_created(
            workflow_id=args.workflow_id,
            task_id=args.task_id,
            babelarr_job_id=args.subtitle_job_id,
        )
        return _workflow_payload(runtime, workflow)

    if args.command == "record-subtitle-job-status":
        workflow = runtime.record_babelarr_job_status(
            workflow_id=args.workflow_id,
            task_id=args.task_id,
            status=args.status,
            status_detail=_parse_optional_json(args.status_detail_json),
            result=_parse_optional_json(args.result_json),
            error=_parse_optional_json(args.error_json),
        )
        return _workflow_payload(runtime, workflow)

    if args.command == "workflow-show":
        workflow = runtime.workflow_summary(args.workflow_id)
        return _workflow_payload(runtime, workflow)

    if args.command == "workflow-list":
        return {
            "status": "ok",
            "runtime_store": str(runtime.root),
            "workflows": runtime.list_workflows(),
        }

    if args.command == "queue-status":
        reconcile_terminal_babelarr_jobs(runtime, job_store_dir=args.job_store_dir)
        return {
            "status": "ok",
            "runtime_store": str(runtime.root),
            "queue": runtime.queue_status(requester_id=args.requester_id),
        }

    raise RuntimeStoreError("unsupported command: %s" % args.command)


def _workflow_payload(runtime: MediaWorkflowRuntime, workflow: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": "ok",
        "runtime_store": str(runtime.root),
        "workflow": workflow,
    }


def _parse_optional_json(value: Optional[str]) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise RuntimeStoreError("JSON value must be an object")
    return parsed


def _completion_event_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    if args.event_json:
        parsed = json.loads(args.event_json)
        if not isinstance(parsed, dict):
            raise RuntimeStoreError("completion event JSON must be an object")
        return parsed
    if args.info_hash and args.content_path:
        return {}
    if sys.stdin.isatty():
        raise RuntimeStoreError(
            "completion event JSON is required on stdin or --event-json when --info-hash and --content-path are absent"
        )
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeStoreError("completion event JSON must be an object")
    return parsed


def _dispatcher_kwargs(args: argparse.Namespace, event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    event = event or {}
    return {
        "limit": args.limit,
        "job_store_dir": args.job_store_dir,
        "notification_target": args.notification_target or _event_string(event, "notification_target"),
        "requester_id": args.requester_id or _event_string(event, "requester_id"),
        "notification_language": args.notification_language or _event_string(event, "notification_language"),
        "backend": args.backend,
        "model": args.model,
        "openai_base_url": args.openai_base_url,
        "openai_api_key": args.openai_api_key,
        "allow_low_confidence_subtitle": args.allow_low_confidence_subtitle,
        "allow_provider_fallback_language": args.allow_provider_fallback_language,
        "opensubtitles_api_key": args.opensubtitles_api_key,
        "opensubtitles_user_agent": args.opensubtitles_user_agent,
        "opensubtitles_username": args.opensubtitles_username,
        "opensubtitles_password": args.opensubtitles_password,
        "opensubtitles_token": args.opensubtitles_token,
        "subdl_api_key": args.subdl_api_key,
    }


def _event_string(event: Dict[str, Any], key: str) -> Optional[str]:
    value = event.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _nested_event_string(event: Dict[str, Any], parent: str, key: str) -> Optional[str]:
    nested = event.get(parent)
    if not isinstance(nested, dict):
        return None
    value = nested.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _event_error(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    error = event.get("error")
    if isinstance(error, dict):
        return dict(error)
    message = _event_string(event, "error")
    if message:
        return {"message": message}
    return None


def _string_value(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _mapped_content_path(
    value: Optional[str],
    *,
    content_path_prefix: Optional[str],
    local_content_path_prefix: Optional[str],
) -> Optional[str]:
    path = _string_value(value)
    if path is None:
        return None
    source_prefix = _string_value(content_path_prefix)
    local_prefix = _string_value(local_content_path_prefix)
    if not source_prefix and not local_prefix:
        return path
    if not source_prefix or not local_prefix:
        raise RuntimeStoreError("content path mapping requires both --content-path-prefix and --local-content-path-prefix")

    source_prefix = source_prefix.rstrip("/") or "/"
    local_prefix = local_prefix.rstrip("/") or "/"
    if path == source_prefix:
        return local_prefix
    if path.startswith(source_prefix + "/"):
        return local_prefix + path[len(source_prefix) :]
    return path


def _error_summary(error: Exception) -> Dict[str, Any]:
    return {
        "status": "error",
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }


def _add_runtime_store_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--runtime-store-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="Runtime workflow store directory.",
    )


def _add_dispatch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--job-store-dir")
    parser.add_argument("--notification-target")
    parser.add_argument("--requester-id")
    parser.add_argument("--notification-language")
    parser.add_argument("--backend", choices=["fake", "codex-cli", "openai-compatible"])
    parser.add_argument("--model")
    parser.add_argument("--openai-base-url")
    parser.add_argument("--openai-api-key")
    parser.add_argument("--allow-low-confidence-subtitle", action="store_true")
    parser.add_argument("--allow-provider-fallback-language", action="store_true")
    parser.add_argument("--opensubtitles-api-key")
    parser.add_argument("--opensubtitles-user-agent")
    parser.add_argument("--opensubtitles-username")
    parser.add_argument("--opensubtitles-password")
    parser.add_argument("--opensubtitles-token")
    parser.add_argument("--subdl-api-key")
    _add_content_path_mapping_options(parser)


def _add_content_path_mapping_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--content-path-prefix",
        default=os.environ.get("BABELARR_RUNTIME_CONTENT_PATH_PREFIX") or os.environ.get("MWR_CONTENT_PATH_PREFIX"),
    )
    parser.add_argument(
        "--local-content-path-prefix",
        default=os.environ.get("BABELARR_RUNTIME_LOCAL_CONTENT_PATH_PREFIX") or os.environ.get("MWR_LOCAL_CONTENT_PATH_PREFIX"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
